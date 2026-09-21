"""Relay API — owner-device egress pairing and status.

Security model:
- status/enable/disable/token/test require full owner auth (X-Auth-Token).
- stream (device → server) and upstream (device → server data) require the
  relay bearer token. That token is purpose-scoped: it can shuttle bytes for
  the browser but can never read research data, reports, or artifacts.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.routes.deps import require_owner_sync

router = APIRouter(prefix="/api/relay")


def _hub(request: Request):
    hub = getattr(request.app.state, "relay", None)
    if hub is None:
        raise HTTPException(status_code=404, detail="relay not initialized")
    return hub


def _auth(request: Request) -> None:
    try:
        require_owner_sync(request)
    except Exception as exc:  # AuthError → 401
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def _relay_token_ok(request: Request, hub) -> bool:
    authz = request.headers.get("authorization", "")
    if authz.lower().startswith("bearer "):
        return hub.check_token(authz[7:])
    return hub.check_token(request.headers.get("x-relay-token"))


@router.get("/status")
async def status(request: Request) -> dict:
    _auth(request)
    return {"relay": _hub(request).status()}


async def _maybe_restart_browser(request: Request) -> bool:
    """Relaunch Chromium so it picks up (or drops) the relay shim as its
    proxy. Never yanks the browser out from under a running research page —
    the new egress applies on the next launch instead. Returns whether a
    restart actually happened."""
    from app.routes.browser import _maybe_restart_browser as _restart

    mgr = getattr(request.app.state, "browser_manager", None)
    interactive = getattr(request.app.state, "interactive", None)
    if mgr is None or interactive is None:
        return False
    if interactive.is_research_busy():
        return False
    try:
        await mgr.launch(force=True)
        return True
    except Exception:
        return False


@router.post("/enable")
async def enable(request: Request, restart_browser: bool = True) -> dict:
    _auth(request)
    hub = _hub(request)
    st = await hub.start()
    restarted = await _maybe_restart_browser(request) if restart_browser else False
    return {"relay": st, "browser_restarted": restarted}


@router.post("/disable")
async def disable(request: Request, restart_browser: bool = True) -> dict:
    _auth(request)
    hub = _hub(request)
    await hub.stop()
    restarted = await _maybe_restart_browser(request) if restart_browser else False
    return {"relay": hub.status(), "browser_restarted": restarted}


@router.get("/token")
async def get_token(request: Request) -> dict:
    """Hand the pairing token to the authenticated owner (never to the device route)."""
    _auth(request)
    return {"token": _hub(request).token()}


@router.post("/token/rotate")
async def rotate_token(request: Request) -> dict:
    _auth(request)
    return {"token": _hub(request).rotate_token()}


@router.get("/stream")
async def stream(request: Request):
    """Device → server downstream (SSE). Requires relay token, not owner auth,
    so the phone client stays simple and purpose-scoped."""
    hub = _hub(request)
    if not _relay_token_ok(request, hub):
        raise HTTPException(status_code=401, detail="relay token required")
    q = hub.attach_device()
    hub.touch_device()

    async def gen():
        try:
            # Initial hello so the client knows the pipe is live.
            yield "retry: 3000\n\n"
            yield f"data: {json.dumps({'op': 'hello', 'mode': hub.status()['mode']})}\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=20)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {msg}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            hub.detach_device(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/upstream")
async def upstream(request: Request) -> dict:
    hub = _hub(request)
    if not _relay_token_ok(request, hub):
        raise HTTPException(status_code=401, detail="relay token required")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    msgs = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(msgs, list):
        raise HTTPException(status_code=400, detail="expected {\"messages\": [...]}")
    handled = await hub.upstream(msgs[:512])
    return {"handled": handled}


@router.post("/test-egress")
async def test_egress(request: Request) -> dict:
    """What IP do sites actually see right now?

    Opens tunnels through the active egress path (device → proxy → direct)
    to two independent plain-HTTP echo services. Plain HTTP is deliberate:
    the tunnel transports raw bytes, so no TLS handshake is needed for the
    origin to observe and report the exit IP.
    """
    _auth(request)
    hub = _hub(request)
    import time as _time

    targets = [("api.ipify.org", "/?format=json"), ("icanhazip.com", "/")]
    results = []
    seen_ip: str | None = None
    for host, path in targets:
        try:
            t0 = _time.monotonic()
            tunnel = await hub.open_tunnel(host, 80)
            req = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}\r\n"
                "User-Agent: Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/126 Mobile Safari/537.36\r\n"
                "Accept: */*\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
            await tunnel.write(req)
            buf = b""
            deadline = _time.monotonic() + 15
            while _time.monotonic() < deadline:
                try:
                    chunk = await asyncio.wait_for(tunnel.read(), timeout=max(0.5, deadline - _time.monotonic()))
                except asyncio.TimeoutError:
                    break
                if not chunk:
                    break
                buf += chunk
                if len(buf) > 65536:
                    break
            await tunnel.close()
            latency_ms = int((_time.monotonic() - t0) * 1000)
            if buf[:4] != b"HTTP":
                results.append({"host": host, "ok": False, "latency_ms": latency_ms, "note": "no HTTP response"})
                continue
            head, _, bodyb = buf.partition(b"\r\n\r\n")
            status_line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            body_text = bodyb.decode("utf-8", "replace").strip()
            results.append({"host": host, "ok": " 200 " in status_line + " ", "latency_ms": latency_ms, "status": status_line, "body": body_text[:300]})
            if host == "api.ipify.org":
                try:
                    seen_ip = json.loads(body_text).get("ip") or seen_ip
                except Exception:
                    if body_text.count(".") == 3:
                        seen_ip = body_text
            elif seen_ip is None and body_text.count(".") == 3:
                seen_ip = body_text.split()[0] if body_text else None
        except Exception as exc:
            results.append({"host": host, "ok": False, "error": str(exc)[:200]})
    path_taken = (
        "owner device" if hub.device_connected
        else ("external proxy (BROWSER_PROXY)" if (getattr(hub.config, "browser_proxy", "") and hub.stats["fallback_proxy"] >= hub.stats["fallback_direct"]) else "direct (server IP)" if hub.stats["fallback_direct"] else "unknown")
    )
    return {
        "device_connected": hub.device_connected,
        "egress_ip": seen_ip,
        "path": path_taken,
        "results": results,
        "stats": dict(hub.stats),
    }
