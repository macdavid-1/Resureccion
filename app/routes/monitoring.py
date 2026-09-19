"""Live monitoring: session state stream, activity trace, browser live view.

The owner opens Resurrección at any moment and watches a running session
WITHOUT manual refresh:

- GET /api/sessions/{id}/stream  — Server-Sent Events with a compact state
  snapshot (phase, progress, counts, latest trace lines, browser status,
  recoveries) pushed on change; resilient to owner disconnects.
- GET /api/sessions/{id}/activity — the concise operational trace (the
  «why» one-liners), pollable with after_id for incremental updates.
- GET /api/sessions/{id}/timing — timing instrumentation (per task type,
  waits, slowest tasks, seconds since last progress).
- GET /api/sessions/{id}/live-view/frame — newest live browser frame (JPEG).
- GET /api/sessions/{id}/live-view/stream — multipart JPEG stream for
  <img src=...>; only produces frames while a watcher is connected.

The research agent does NOT depend on any of this: if nobody is watching,
nothing is captured and research continues identically.
"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from app.live_view import LiveViewStore
from app.routes.deps import require_owner_sync
from app.timing import TimingTracker
from app.trace import ActivityTrace

router = APIRouter(prefix="/api/sessions", tags=["monitoring"])

# How often the SSE loop wakes to diff state (seconds). Cheap on 2 cores.
_STREAM_INTERVAL = 2.0
# Idle live-view frame cadence so a just-opened page shows something recent.
_LIVEVIEW_IDLE_SECONDS = 10.0


def _auth(request: Request) -> None:
    from app.security import AuthError

    try:
        require_owner_sync(request)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


def _snapshot(request: Request, session_id: str, after_trace: int) -> dict:
    """Compact state snapshot for the live UI. Read-only, cheap."""
    st = request.app.state
    session = st.sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    trace = ActivityTrace(st.db)
    entries = trace.recent(session_id, after_id=after_trace, limit=30)
    counts = st.sessions.counts(session_id)
    agent = st.agent.get(session_id)
    browser = st.browser.get(session_id)
    errors = []
    try:
        with st.db.read() as conn:
            rows = conn.execute(
                "SELECT scope, severity, message, created_at, recoverable FROM error_states "
                "WHERE session_id = ? ORDER BY id DESC LIMIT 5",
                (session_id,),
            ).fetchall()
        errors = [dict(r) for r in rows]
    except Exception:
        errors = []
    last_cp = st.checkpoints.latest(session_id)
    job = st.autonomy.current_job(session_id) if hasattr(st, "autonomy") else None
    return {
        "session": session.to_dict(),
        "counts": counts,
        "agent": agent.to_dict() if agent else None,
        "browser": browser.to_dict() if browser else None,
        "job": job.to_dict() if job else None,
        "trace": [e.to_dict() for e in entries],
        "last_checkpoint": last_cp.to_dict() if last_cp else None,
        "recent_errors": [
            {"scope": e.scope, "severity": e.severity, "message": e.message,
             "created_at": e.created_at, "recoverable": e.recoverable}
            for e in errors
        ],
    }


@router.get("/{session_id}/activity")
async def activity(session_id: str, request: Request, after_id: int = 0, limit: int = 50) -> dict:
    _auth(request)
    st = request.app.state
    if st.sessions.get(session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    trace = ActivityTrace(st.db)
    entries = trace.recent(session_id, after_id=after_id, limit=limit)
    return {"trace": [e.to_dict() for e in entries]}


@router.get("/{session_id}/timing")
async def timing(session_id: str, request: Request) -> dict:
    _auth(request)
    st = request.app.state
    if st.sessions.get(session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    tracker = TimingTracker(st.db, session_id)
    return tracker.session_summary()


@router.get("/{session_id}/stream")
async def stream(session_id: str, request: Request, max_events: int = 0) -> Response:
    """SSE state stream. Research NEVER depends on this: a watcher merely
    observes; disconnects are handled silently. `max_events` bounds the
    number of payloads for thin clients (0 = unbounded)."""
    _auth(request)
    st = request.app.state
    if st.sessions.get(session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    trace = ActivityTrace(st.db)

    async def gen():
        after_trace = 0
        last_payload: str = ""
        heartbeat_deadline = 15.0
        last_sent = asyncio.get_event_loop().time()
        sent = 0
        while True:
            if await request.is_disconnected():
                break
            try:
                snap = _snapshot(request, session_id, after_trace)
                if snap["trace"]:
                    after_trace = snap["trace"][-1]["id"]
                payload = json.dumps(snap, ensure_ascii=False)
                now = asyncio.get_event_loop().time()
                if payload != last_payload or (now - last_sent) >= heartbeat_deadline:
                    yield f"data: {payload}\n\n"
                    last_payload = payload
                    last_sent = now
                    sent += 1
                    if max_events and sent >= max_events:
                        return
            except Exception:
                # Snapshot errors (e.g. mid-write DB) are transient; keep the
                # stream alive and retry next tick.
                pass
            await asyncio.sleep(_STREAM_INTERVAL)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{session_id}/live-view/frame")
async def live_frame(session_id: str, request: Request) -> Response:
    _auth(request)
    st = request.app.state
    if st.sessions.get(session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    lv: LiveViewStore = st.live_view
    frame = lv.latest(session_id)
    if frame is None:
        return Response(status_code=204)
    return Response(
        content=frame.data,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store", "X-Frame-Url": frame.url, "X-Frame-Title": frame.title},
    )


@router.post("/{session_id}/recording/start")
async def recording_start(session_id: str, request: Request) -> dict:
    _auth(request)
    st = request.app.state
    if st.sessions.get(session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    rec = st.recorder.start(session_id)
    return {"recording": rec}


@router.post("/{session_id}/recording/stop")
async def recording_stop(session_id: str, request: Request) -> dict:
    _auth(request)
    st = request.app.state
    if st.sessions.get(session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    rec = await st.recorder.stop(session_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="no recording for this session")
    return {"recording": rec}


@router.get("/{session_id}/recording")
async def recording_status(session_id: str, request: Request) -> dict:
    _auth(request)
    st = request.app.state
    if st.sessions.get(session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    rec = st.recorder.status(session_id)
    return {"recording": rec}


@router.get("/{session_id}/live-view/stream")
async def live_stream(session_id: str, request: Request) -> Response:
    """Multipart MJPEG-style stream for <img>. While a watcher is connected,
    a fresh browser screenshot is captured at a bounded cadence. Frames go
    through the same bounded ring; nothing grows unboundedly."""
    _auth(request)
    st = request.app.state
    if st.sessions.get(session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    lv: LiveViewStore = st.live_view

    async def gen():
        boundary = "frame"
        yield f"--{boundary}\r\n".encode()
        last = asyncio.get_event_loop().time()
        while True:
            if await request.is_disconnected():
                break
            try:
                png = await st.browser_manager.live_screenshot(session_id)
                if png:
                    from app.live_view import downscale_jpeg

                    jpeg = downscale_jpeg(png)
                    if jpeg:
                        lv.put(session_id, jpeg)
                        yield (
                            f"--{boundary}\r\nContent-Type: image/jpeg\r\n"
                            f"Content-Length: {len(jpeg)}\r\n\r\n"
                        ).encode() + jpeg + b"\r\n"
                        last = asyncio.get_event_loop().time()
                        await asyncio.sleep(1.5)
                        continue
            except Exception:
                pass
            # No live browser: emit the newest stored frame or a placeholder
            # heartbeat at the idle cadence, keeping the connection cheap.
            if asyncio.get_event_loop().time() - last >= _LIVEVIEW_IDLE_SECONDS:
                yield f"--{boundary}\r\nContent-Type: text/plain\r\n\r\nidle\r\n".encode()
                last = asyncio.get_event_loop().time()
            await asyncio.sleep(_LIVEVIEW_IDLE_SECONDS / 2)

    return StreamingResponse(
        gen(),
        media_type=f"multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store"},
    )
