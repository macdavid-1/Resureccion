"""Research session routes — the primary owner-facing API."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.routes.deps import require_owner_sync
from app.security import AuthError

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _auth(request: Request) -> None:
    try:
        require_owner_sync(request)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


class CreateSessionRequest(BaseModel):
    mode: str = Field(description="prompt | keywords | auto")
    prompt: str = ""
    objective: str = ""
    marketplaces: list[str] = Field(default_factory=list)


class UpdateSessionRequest(BaseModel):
    name: str | None = None
    objective: str | None = None
    progress: float | None = None


@router.get("")
async def list_sessions(request: Request, include_terminal: bool = True) -> dict:
    _auth(request)
    st = request.app.state
    rows = st.sessions.list(include_terminal=include_terminal)
    # Batched counters: one grouped query for the whole archive (mobile).
    counts = st.sessions.counts_many([s.id for s in rows])
    out = []
    for s in rows:
        d = s.to_dict()
        d["counts"] = counts.get(s.id)
        out.append(d)
    return {"sessions": out}


@router.post("")
async def create_session(body: CreateSessionRequest, request: Request) -> dict:
    _auth(request)
    st = request.app.state
    try:
        s = st.sessions.create(
            mode=body.mode,
            prompt=body.prompt,
            objective=body.objective,
            marketplaces=body.marketplaces,
        )
        st.agent.initialize(s.id)
        st.browser.ensure(s.id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    data = s.to_dict()
    agent = st.agent.get(s.id)
    data["agent"] = agent.to_dict() if agent else None
    browser = st.browser.get(s.id)
    data["browser"] = browser.to_dict() if browser else None
    return {"session": data}


@router.get("/{session_id}")
async def get_session(session_id: str, request: Request) -> dict:
    _auth(request)
    st = request.app.state
    s = st.sessions.get(session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")
    data = s.to_dict()
    data["counts"] = st.sessions.counts(session_id)
    agent = st.agent.get(session_id)
    data["agent"] = agent.to_dict() if agent else None
    browser = st.browser.get(session_id)
    data["browser"] = browser.to_dict() if browser else None
    return {"session": data}


@router.patch("/{session_id}")
async def update_session(session_id: str, body: UpdateSessionRequest, request: Request) -> dict:
    _auth(request)
    st = request.app.state
    try:
        s = st.sessions.update(
            session_id,
            name=body.name,
            objective=body.objective,
            progress=body.progress,
        )
    except Exception as exc:
        raise HTTPException(status_code=404 if "not found" in str(exc) else 400, detail=str(exc)) from exc
    return {"session": s.to_dict()}


@router.post("/{session_id}/start")
async def start_session(session_id: str, request: Request) -> dict:
    _auth(request)
    st = request.app.state
    try:
        s = st.sessions.require(session_id)
        if s.status in ("completed", "failed", "cancelled"):
            raise ValueError(f"cannot start session in status {s.status!r}")
        s = st.sessions.update(session_id, status="queued", error=None)
        st.jobs.create_job(session_id, kind="research_run", payload={})
        st.runners.start(session_id)
    except Exception as exc:
        raise HTTPException(
            status_code=404 if "not found" in str(exc) else 400, detail=str(exc)
        ) from exc
    return {"session": s.to_dict()}


@router.post("/{session_id}/pause")
async def pause_session(session_id: str, request: Request) -> dict:
    _auth(request)
    st = request.app.state
    try:
        s = st.sessions.require(session_id)
        if s.status != "running":
            raise ValueError("only running sessions can be paused")
        # Cooperative pause: the live runner checkpoints and stops at the next
        # safe boundary (never mid-page, never mid-write).
        runner = st.runners._runners.get(session_id)
        if runner is not None:
            runner.request_pause()
        st.autonomy.mark(session_id, "paused", detail={"note": "owner requested pause"})
        s = st.sessions.update(session_id, status="paused")
    except Exception as exc:
        raise HTTPException(
            status_code=404 if "not found" in str(exc) else 400, detail=str(exc)
        ) from exc
    return {"session": s.to_dict()}


@router.post("/{session_id}/resume")
async def resume_session(session_id: str, request: Request) -> dict:
    _auth(request)
    st = request.app.state
    try:
        s = st.sessions.require(session_id)
        if s.status not in ("interrupted", "paused"):
            raise ValueError(f"cannot resume session in status {s.status!r}")
        st.sessions.bump_resume(session_id)
        s = st.sessions.update(session_id, status="queued", error=None)
        st.jobs.create_job(session_id, kind="research_resume", payload={})
        st.autonomy.mark(session_id, "queued", detail={"note": "owner resumed"})
        st.runners.start(session_id)
    except Exception as exc:
        raise HTTPException(
            status_code=404 if "not found" in str(exc) else 400, detail=str(exc)
        ) from exc
    return {"session": s.to_dict()}


@router.post("/{session_id}/cancel")
async def cancel_session(session_id: str, request: Request) -> dict:
    _auth(request)
    st = request.app.state
    try:
        s = st.sessions.require(session_id)
        if s.status in ("completed", "cancelled"):
            raise ValueError(f"cannot cancel session in status {s.status!r}")
        for job in st.jobs.active_jobs(session_id):
            st.jobs.cancel_job(session_id, job.id)
        st.runners.cancel(session_id)  # asks live runner to stop; durable state already committed
        st.autonomy.mark(session_id, "cancelled", detail={"note": "owner cancelled"})
        s = st.sessions.update(session_id, status="cancelled")
        st.live_view.clear(session_id)
    except Exception as exc:
        raise HTTPException(
            status_code=404 if "not found" in str(exc) else 400, detail=str(exc)
        ) from exc
    return {"session": s.to_dict()}


@router.get("/{session_id}/jobs")
async def list_jobs(session_id: str, request: Request, status: str | None = None) -> dict:
    _auth(request)
    st = request.app.state
    try:
        st.sessions.require(session_id)
        jobs = st.jobs.list_jobs(session_id, status=status)
    except Exception as exc:
        raise HTTPException(
            status_code=404 if "not found" in str(exc) else 400, detail=str(exc)
        ) from exc
    return {"jobs": [j.to_dict() for j in jobs]}


@router.get("/{session_id}/observations")
async def list_observations(session_id: str, request: Request, limit: int = 100, offset: int = 0) -> dict:
    _auth(request)
    st = request.app.state
    if st.sessions.get(session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    obs = st.observations.list(session_id, limit=min(limit, 500), offset=max(0, offset))
    return {"observations": [o.to_dict() for o in obs]}


@router.get("/{session_id}/candidates")
async def list_candidates(session_id: str, request: Request, status: str | None = None) -> dict:
    _auth(request)
    st = request.app.state
    if st.sessions.get(session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    try:
        cands = st.candidates.list(session_id, status=status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"candidates": [c.to_dict() for c in cands]}


@router.get("/{session_id}/opportunities")
async def list_opportunities(session_id: str, request: Request) -> dict:
    _auth(request)
    st = request.app.state
    if st.sessions.get(session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    opps = st.opportunities.list(session_id)
    return {"opportunities": [o.to_dict() for o in opps]}


@router.get("/{session_id}/verifications")
async def list_verifications(session_id: str, request: Request) -> dict:
    _auth(request)
    st = request.app.state
    if st.sessions.get(session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    vers = st.verifications.list(session_id)
    return {"verifications": [v.to_dict() for v in vers]}


@router.get("/{session_id}/checkpoints")
async def list_checkpoints(session_id: str, request: Request) -> dict:
    _auth(request)
    st = request.app.state
    if st.sessions.get(session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    cps = st.checkpoints.list(session_id)
    return {"checkpoints": [c.to_dict() for c in cps]}


@router.get("/{session_id}/errors")
async def list_errors(session_id: str, request: Request) -> dict:
    _auth(request)
    st = request.app.state
    if st.sessions.get(session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {"errors": st.errors.unresolved(session_id)}
