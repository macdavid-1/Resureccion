"""FastAPI application factory and wiring."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.agent import AgentStateStore
from app.amazon_auth import AmazonAuthManager
from app.artifacts import ArtifactStore
from app.autonomy import AutonomySupervisor
from app.claims import ClaimStore
from app.integrity import IntegrityAssessmentStore
from app.live_view import LiveViewStore
from app.browser import BrowserRegistry
from app.browser_manager import BrowserManager
from app.browser_store import (
    BrowserAuthStore,
    BrowserEvidenceStore,
    ExtensionStore,
    LoginWindowStore,
)
from app.config import get_config
from app.db import init_db
from app.events import EventLog
from app.exports import ExportManager
from app.jobs import JobOrchestrator
from app.kdspy import KDSpyManager
from app.methodology import Methodology
from app.recovery import CheckpointStore, ErrorStateStore, RecoveryManager
from app.recording import SessionRecorder
from app.reports import ReportStore
from app.research_data import (
    CandidateStore,
    EvidenceStore,
    ObservationStore,
    OpportunityStore,
    VerificationStore,
)
from app.routes import auth as auth_routes
from app.routes import browser as browser_routes
from app.routes import misc as misc_routes
from app.routes import monitoring as monitoring_routes
from app.routes import sessions as session_routes
from app.trace import ActivityTrace
from app.runner_registry import RunnerRegistry
from app.security import AuthError, AuthService
from app.sessions import SessionStore
from app.uploads import UploadStore

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def create_app() -> FastAPI:
    config = get_config()
    config.ensure_dirs()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Boot: DB, stores, single-user, crash recovery.
        db = init_db(config)
        app.state.db = db
        app.state.config = config
        app.state.events = EventLog(db)
        app.state.sessions = SessionStore(db, config)
        app.state.agent = AgentStateStore(db, app.state.events)
        app.state.jobs = JobOrchestrator(db, config, app.state.sessions, app.state.events, _errors := ErrorStateStore(db))
        app.state.errors = _errors
        app.state.checkpoints = CheckpointStore(db)
        app.state.browser = BrowserRegistry(db, app.state.sessions)
        app.state.observations = ObservationStore(db)
        app.state.evidence = EvidenceStore(db)
        app.state.candidates = CandidateStore(db)
        app.state.verifications = VerificationStore(db)
        app.state.opportunities = OpportunityStore(db)
        app.state.claims = ClaimStore(db)
        app.state.integrity = IntegrityAssessmentStore(db)
        app.state.artifacts = ArtifactStore(db, config)
        app.state.uploads = UploadStore(db, config)
        app.state.reports = ReportStore(db, config)
        app.state.exports = ExportManager(
            db,
            config,
            app.state.reports,
            app.state.candidates,
            app.state.opportunities,
            observations=app.state.observations,
            evidence=app.state.evidence,
            events=app.state.events,
            checkpoints=app.state.checkpoints,
        )

        # --- browser infrastructure (real Chromium + Playwright) -------------
        app.state.browser_auth = BrowserAuthStore(db)
        app.state.extension_states = ExtensionStore(db)
        app.state.login_windows = LoginWindowStore(db)
        app.state.browser_evidence = BrowserEvidenceStore(db)
        app.state.kdspy = KDSpyManager(config, app.state.extension_states)
        app.state.browser_manager = BrowserManager(config, app.state.kdspy, app.state.browser_evidence)
        app.state.amazon_auth = AmazonAuthManager(
            config, app.state.browser_manager, app.state.browser_auth, app.state.login_windows
        )
        app.state.kdspy.validate_installation()  # record pre-launch state
        app.state.login_windows.expire_stale()

        # --- research executor (methodology + model + browser actions) ------
        app.state.methodology = Methodology(config)
        app.state.trace = ActivityTrace(db)
        app.state.live_view = LiveViewStore(db)
        app.state.autonomy = AutonomySupervisor(
            db,
            config,
            sessions=app.state.sessions,
            events=app.state.events,
            errors=app.state.errors,
            checkpoints=app.state.checkpoints,
        )
        app.state.runners = RunnerRegistry(
            db,
            config,
            sessions=app.state.sessions,
            events=app.state.events,
            errors=app.state.errors,
            state=app.state,
        )
        app.state.recorder = SessionRecorder(
            db,
            config,
            live_view=app.state.live_view,
            artifacts=app.state.artifacts,
            trace=app.state.trace,
            browser_manager=app.state.browser_manager,
        )

        app.state.auth = AuthService(db, config)
        # Seed the owner row only when credentials are configured. In dev/preview
        # without OWNER_PASSWORD_HASH the app still boots (login is impossible,
        # so nothing is exposed); production must set the env or login fails.
        if config.owner_password_hash:
            app.state.auth.ensure_user()
        app.state.jobs.reconcile_on_boot()
        app.state.exports.requeue_interrupted()
        recovered = RecoveryManager(db, app.state.sessions, app.state.checkpoints, app.state.errors).recover_all()
        if recovered:
            for sid in recovered:
                app.state.events.append(
                    sid, level="warn", actor="system", action="session_recovered_after_restart", detail={}
                )
        # Autonomy: recover durable jobs after restart and auto-resume the
        # sessions that have a safe checkpoint — the owner need not be online.
        try:
            autonomy_counts = app.state.autonomy.recover_on_boot()
            resumed = app.state.autonomy.auto_resume_recovered(app.state.runners)
            if autonomy_counts.get("resumed") or resumed:
                print(
                    f"[autonomy] boot recovery {autonomy_counts}; auto-resumed {resumed}",
                    flush=True,
                )
        except Exception as exc:
            print(f"[autonomy] boot recovery error: {exc}", flush=True)
        # Recordings interrupted by a restart are finalized from the frames
        # already on disk — hours of footage are never silently lost.
        try:
            rec_recovered = app.state.recorder.recover_on_boot()
            if rec_recovered:
                print(f"[recording] recovered {len(rec_recovered)} interrupted recording(s)", flush=True)
        except Exception as exc:
            print(f"[recording] boot recovery error: {exc}", flush=True)
        yield
        await app.state.browser_manager.shutdown()
        db.close()

    app = FastAPI(title="Resurrección", version="0.3.0", lifespan=lifespan)

    app.include_router(auth_routes.router)
    app.include_router(session_routes.router)
    app.include_router(misc_routes.router)
    app.include_router(browser_routes.router)
    app.include_router(monitoring_routes.router)

    @app.exception_handler(AuthError)
    async def auth_error_handler(_: Request, exc: AuthError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"detail": str(exc)})

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True}

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
