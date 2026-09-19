"""Runner registry: owns live ResearchRunner asyncio tasks.

- `start(session_id)` spawns a runner task for a queued/running session,
  enforcing the configured concurrency cap (MAX_CONCURRENT_SESSIONS).
- `cancel(session_id)` asks a live runner to stop (durable cancel flow).
- On process boot, no runners exist — sessions left 'running' are marked
  interrupted by RecoveryManager and can be resumed (which spawns a runner).
"""
from __future__ import annotations

import asyncio
from typing import Any

from app.config import Config
from app.db import Database
from app.events import EventLog
from app.recovery import ErrorStateStore
from app.research_runner import ResearchRunner, RunnerConfig
from app.sessions import SessionStore


class RunnerRegistryError(Exception):
    pass


class RunnerRegistry:
    def __init__(
        self,
        db: Database,
        config: Config,
        *,
        sessions: SessionStore,
        events: EventLog,
        errors: ErrorStateStore,
        state: dict[str, Any],
        runner_config: RunnerConfig | None = None,
    ) -> None:
        self.db = db
        self.config = config
        self.sessions = sessions
        self.events = events
        self.errors = errors
        self.state = state  # app.state (for wiring shared stores)
        self.runner_config = runner_config or RunnerConfig()
        self._runners: dict[str, ResearchRunner] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------ build
    def _build_runner(self) -> ResearchRunner:
        st = self.state
        from app.model_client import ModelClient

        return ResearchRunner(
            self.db,
            self.config,
            sessions=st.sessions,
            agent=st.agent,
            events=st.events,
            errors=st.errors,
            checkpoints=st.checkpoints,
            candidates=st.candidates,
            evidence=st.evidence,
            observations=st.observations,
            opportunities=st.opportunities,
            artifacts=st.artifacts,
            browser=st.browser_manager,
            browser_evidence=st.browser_evidence,
            browser_auth=st.browser_auth,
            model=ModelClient(self.config, st.events),
            runner_config=self.runner_config,
            claims=getattr(st, "claims", None),
            assessments=getattr(st, "integrity", None),
            trace=getattr(st, "trace", None),
            supervisor=getattr(st, "autonomy", None),
        )

    # ------------------------------------------------------------------ spawn
    def is_running(self, session_id: str) -> bool:
        runner = self._runners.get(session_id)
        return runner is not None and not runner._cancelled

    def active_count(self) -> int:
        return sum(1 for sid in self._runners if self.is_running(sid))

    def start(self, session_id: str) -> None:
        if self.is_running(session_id):
            return
        if self.active_count() >= self.config.max_concurrent_sessions:
            raise RunnerRegistryError(
                f"concurrent research limit reached ({self.config.max_concurrent_sessions}); "
                "wait for a running session to finish or raise MAX_CONCURRENT_SESSIONS"
            )
        session = self.sessions.require(session_id)
        if session.status not in ("queued", "running", "interrupted", "paused"):
            raise RunnerRegistryError(
                f"cannot start research for session in status {session.status!r}"
            )
        runner = self._build_runner()
        self._runners[session_id] = runner
        task = asyncio.create_task(runner.run(session_id))
        self._tasks[session_id] = task

        def _done(t: asyncio.Task) -> None:
            self._runners.pop(session_id, None)
            self._tasks.pop(session_id, None)

        task.add_done_callback(_done)
        self.events.append(
            session_id, level="info", actor="system", action="runner_spawned", detail={},
        )

    # ----------------------------------------------------------------- cancel
    def cancel(self, session_id: str) -> bool:
        runner = self._runners.get(session_id)
        if runner is None:
            return False
        runner.cancel()
        return True

    def status(self) -> dict[str, Any]:
        return {
            "active": self.active_count(),
            "limit": self.config.max_concurrent_sessions,
            "sessions": sorted(self._runners.keys()),
        }
