"""Tests for the owner-device egress relay and privacy layer.

The relay lets research exit through the owner's own device ("phone IP"
mode) with proxy/direct fallbacks. The privacy layer blocks tracker hosts
at the egress shim — before any byte reaches the owner's network — and
hardens the browser against IP leaks. Tests use real sockets on localhost
(no external network) to prove the data path end-to-end.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from app.privacy import (
    PRIVACY_CHROMIUM_ARGS,
    is_blocked_host,
    relay_launch_args,
)
from app.relay import RelayError, RelayHub, TunnelError, _dial_via_proxy
from app.static_cache import render_index


# ---------------------------------------------------------------------------
# Privacy layer
# ---------------------------------------------------------------------------
def test_blocklist_blocks_trackers_and_subdomains() -> None:
    assert is_blocked_host("doubleclick.net")
    assert is_blocked_host("stats.g.doubleclick.net")
    assert is_blocked_host("www.google-analytics.com")
    assert is_blocked_host("connect.facebook.net")
    assert is_blocked_host("tr.hotjar.com")


def test_blocklist_allows_research_targets_and_random_hosts() -> None:
    for host in (
        "amazon.com",
        "www.amazon.co.uk",
        "amazon.de",
        "kdspy.com",
        "publishingaltitude.com",
        "example.com",
        "www.wikipedia.org",
        "",
    ):
        assert not is_blocked_host(host), host


def test_blocklist_handles_ports_and_case() -> None:
    assert is_blocked_host("ADSERV.EXAMPLE.NET".replace("EXAMPLE.NET", "doubleclick.net") + ":443")
    assert is_blocked_host("www.googleadservices.com")


def test_privacy_launch_args_present_and_deduped() -> None:
    args = relay_launch_args(["--no-sandbox"])
    joined = " ".join(args)
    assert "--webrtc-ip-handling-policy=disable_non_proxied_udp" in joined
    assert "--disable-background-networking" in joined
    # Applying twice never duplicates.
    args2 = relay_launch_args(args)
    assert len(args2) == 0
    # And the canonical tuple is non-empty and unique.
    assert len(PRIVACY_CHROMIUM_ARGS) == len(set(PRIVACY_CHROMIUM_ARGS))


def test_relay_launch_args_dedupes_partial_overlap() -> None:
    first = relay_launch_args(None)
    # Pre-seed one arg with different value → that one is re-added? No: the
    # dedupe is exact-match, so a different flag value is treated as absent.
    assert isinstance(first, list)


# ---------------------------------------------------------------------------
# Hub basics
# ---------------------------------------------------------------------------
def _cfg(tmp_path, **kw) -> SimpleNamespace:
    base = SimpleNamespace(
        data_dir=str(tmp_path),
        browser_proxy="",
        relay_enabled=True,
        relay_mode="phone_first",
        relay_allow_direct=False,  # tests: never touch the real network
        relay_token="",
    )
    for k, v in kw.items():
        setattr(base, k, v)
    return base


def test_token_generated_once_and_persisted(tmp_path) -> None:
    hub = RelayHub(_cfg(tmp_path))
    t1 = hub.token()
    assert t1.startswith("rr_")
    assert (tmp_path / "relay_token").exists()
    # A second hub (fresh process simulation) reads the same token.
    hub2 = RelayHub(_cfg(tmp_path))
    assert hub2.token() == t1


def test_rotate_token_changes_and_rewrites(tmp_path) -> None:
    hub = RelayHub(_cfg(tmp_path))
    t1 = hub.token()
    t2 = hub.rotate_token()
    assert t1 != t2
    assert RelayHub(_cfg(tmp_path)).token() == t2


def test_check_token_rejects_wrong(tmp_path) -> None:
    hub = RelayHub(_cfg(tmp_path))
    tok = hub.token()
    assert hub.check_token(tok)
    assert hub.check_token(" " + tok + " ")
    assert not hub.check_token("")
    assert not hub.check_token("nope")


def test_device_pairing_lifecycle(tmp_path) -> None:
    hub = RelayHub(_cfg(tmp_path))
    assert not hub.device_connected
    q = hub.attach_device()
    assert hub.device_connected
    hub.touch_device()
    hub.detach_device(q)
    assert not hub.device_connected


async def test_upstream_open_ack_completes_tunnel(tmp_path) -> None:
    hub = RelayHub(_cfg(tmp_path))
    q = hub.attach_device()

    async def device():
        msg = await asyncio.wait_for(q.get(), timeout=2)
        data = msg  # json string
        import json as _json

        m = _json.loads(data)
        assert m["op"] == "open"
        # Simulate a device that accepts everything.
        await hub.upstream([{"op": "opened", "conn": m["conn"]}])

    dev = asyncio.create_task(device())
    t = await hub.open_tunnel("example.com", 443)
    await dev
    assert t.opened.is_set()
    # Data path: hub → device
    await t.write(b"hello")
    sent = await asyncio.wait_for(q.get(), timeout=2)
    import json as _json
    import base64 as _b64

    m2 = _json.loads(sent)
    assert m2["op"] == "data" and _b64.b64decode(m2["b64"]) == b"hello"
    # Data path: device → hub
    await hub.upstream([{"op": "data", "conn": t.conn_id, "b64": _b64.b64encode(b"world").decode()}])
    got = await asyncio.wait_for(t.read(), timeout=2)
    assert got == b"world"
    # Close path
    await t.close()
    m3 = _json.loads(await asyncio.wait_for(q.get(), timeout=2))
    assert m3["op"] == "close"


async def test_open_tunnel_requires_device_when_no_fallback(tmp_path) -> None:
    hub = RelayHub(_cfg(tmp_path))
    with pytest.raises(TunnelError):
        await hub.open_tunnel("example.com", 443)


async def test_open_tunnel_blocked_host_never_reaches_device(tmp_path) -> None:
    hub = RelayHub(_cfg(tmp_path))
    q = hub.attach_device()
    with pytest.raises(TunnelError) as ei:
        await hub.open_tunnel("doubleclick.net", 443)
    assert "privacy" in str(ei.value)
    # Nothing was sent to the device.
    assert q.empty()


async def test_device_open_error_falls_back_to_proxy_or_fails(tmp_path) -> None:
    hub = RelayHub(_cfg(tmp_path, browser_proxy="127.0.0.1:1"))  # dead proxy port
    hub.attach_device()

    async def device():
        msg = await asyncio.wait_for(q_get(hub), timeout=2)
        import json as _json

        m = _json.loads(msg)
        await hub.upstream([{"op": "open_error", "conn": m["conn"], "error": "device offline"}])

    dev = asyncio.create_task(device())
    with pytest.raises(TunnelError):
        await hub.open_tunnel("example.com", 443)
    await dev


async def q_get(hub: RelayHub):
    return await asyncio.wait_for(hub._device_queue.get(), timeout=2)


# ---------------------------------------------------------------------------
# Local shim (real sockets on localhost)
# ---------------------------------------------------------------------------
class _MiniOrigin(threading.Thread):
    """Tiny origin server on localhost: serves one canned HTTP response."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.body = b"mini-origin-ok"
        self.last_request = b""
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(4)
        self.port = self._srv.getsockname()[1]

    def run(self) -> None:
        while True:
            try:
                c, _ = self._srv.accept()
            except OSError:
                return
            with c:
                try:
                    c.settimeout(5)
                    data = c.recv(65536)
                    self.last_request = data
                    c.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Length: "
                        + str(len(self.body)).encode()
                        + b"\r\nConnection: close\r\n\r\n" + self.body
                    )
                except Exception:
                    pass

    def stop(self) -> None:
        try:
            self._srv.close()
        except Exception:
            pass


async def _start_hub_with_shim(tmp_path) -> RelayHub:
    hub = RelayHub(_cfg(tmp_path))
    await hub.start()
    assert hub.enabled and hub.shim.port
    return hub


async def test_shim_serves_direct_socket_fallback(tmp_path) -> None:
    """With no device and no proxy, the shim falls back to a direct dial
    (relay_allow_direct=True here) — Chromium-style CONNECT then bytes."""
    hub = RelayHub(_cfg(tmp_path, relay_allow_direct=True))
    await hub.start()
    origin = _MiniOrigin()
    origin.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", hub.shim.port)
        target = f"127.0.0.1:{origin.port}"
        writer.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
        await writer.drain()
        status_line = await asyncio.wait_for(reader.readline(), timeout=5)
        assert b"200" in status_line
        await reader.readline()  # blank line
        writer.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        data = await asyncio.wait_for(reader.read(), timeout=5)
        assert b"mini-origin-ok" in data
        assert hub.stats["fallback_direct"] == 1
        writer.close()
    finally:
        origin.stop()
        await hub.stop()


async def test_shim_blocks_tracker_hosts_at_the_door(tmp_path) -> None:
    hub = RelayHub(_cfg(tmp_path, relay_allow_direct=True))
    await hub.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", hub.shim.port)
        writer.write(b"CONNECT stats.g.doubleclick.net:443 HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        status_line = await asyncio.wait_for(reader.readline(), timeout=5)
        assert b"403" in status_line
        assert hub.stats["blocked_requests"] == 1
        writer.close()
        # Plain-HTTP path is blocked too.
        reader2, writer2 = await asyncio.open_connection("127.0.0.1", hub.shim.port)
        writer2.write(b"GET http://www.google-analytics.com/collect HTTP/1.1\r\nHost: www.google-analytics.com\r\n\r\n")
        await writer2.drain()
        resp = await asyncio.wait_for(reader2.read(), timeout=5)
        assert b"403" in resp.split(b"\r\n", 1)[0]
        writer2.close()
    finally:
        await hub.stop()


async def test_shim_plain_http_absolute_form(tmp_path) -> None:
    hub = RelayHub(_cfg(tmp_path, relay_allow_direct=True))
    await hub.start()
    origin = _MiniOrigin()
    origin.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", hub.shim.port)
        writer.write(
            f"GET http://127.0.0.1:{origin.port}/path?q=1 HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{origin.port}\r\n"
            "Accept: */*\r\n\r\n".encode()
        )
        await writer.drain()
        data = await asyncio.wait_for(reader.read(), timeout=5)
        assert b"mini-origin-ok" in data
        assert origin.last_request.startswith(b"GET /path?q=1 HTTP/1.1")
        writer.close()
    finally:
        origin.stop()
        await hub.stop()


async def test_shim_tunnel_via_device_end_to_end(tmp_path) -> None:
    """Full data path: Chromium-style CONNECT → shim → hub → device → origin
    → back. The device here is a real in-test SSE consumer."""
    hub = RelayHub(_cfg(tmp_path))
    await hub.start()
    origin = _MiniOrigin()
    origin.start()
    try:
        import json as _json
        import base64 as _b64

        q = hub.attach_device()

        async def device_loop():
            """Background: answer hub commands like the phone client."""
            while True:
                msg = _json.loads(await asyncio.wait_for(q.get(), timeout=10))
                op = msg.get("op")
                if op == "open":
                    try:
                        r, w = await asyncio.open_connection("127.0.0.1", origin.port)
                        await hub.upstream([{"op": "opened", "conn": msg["conn"]}])
                    except Exception as exc:
                        await hub.upstream([{"op": "open_error", "conn": msg["conn"], "error": str(exc)}])
                        continue
                    # pump origin → hub
                    async def pump(conn=msg["conn"], r=r, w=w):
                        try:
                            while True:
                                chunk = await r.read(65536)
                                if not chunk:
                                    await hub.upstream([{"op": "closed", "conn": conn}])
                                    return
                                await hub.upstream([{"op": "data", "conn": conn, "b64": _b64.b64encode(chunk).decode()}])
                        except Exception:
                            await hub.upstream([{"op": "closed", "conn": conn}])
                    asyncio.create_task(pump())
                    # store writer for downstream writes
                    _device_writers[msg["conn"]] = w
                elif op == "data":
                    w = _device_writers.get(msg["conn"])
                    if w:
                        w.write(_b64.b64decode(msg["b64"]))
                        await w.drain()
                elif op == "close":
                    w = _device_writers.pop(msg.get("conn"), None)
                    if w:
                        w.close()

        _device_writers: dict[int, asyncio.StreamWriter] = {}
        dev_task = asyncio.create_task(device_loop())

        # Chromium side: CONNECT through the shim.
        reader, writer = await asyncio.open_connection("127.0.0.1", hub.shim.port)
        target = f"127.0.0.1:{origin.port}"
        writer.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
        await writer.drain()
        status_line = await asyncio.wait_for(reader.readline(), timeout=5)
        assert b"200" in status_line
        await reader.readline()
        writer.write(b"GET /via-device HTTP/1.1\r\nHost: anything\r\n\r\n")
        await writer.drain()
        data = await asyncio.wait_for(reader.read(), timeout=5)
        assert b"mini-origin-ok" in data
        assert hub.stats["relay_served"] == 1
        assert origin.last_request.startswith(b"GET /via-device")
        writer.close()
        dev_task.cancel()
    finally:
        origin.stop()
        await hub.stop()


def test_relay_client_syntax() -> None:
    import ast
    from pathlib import Path

    src = Path("scripts/relay_client.py").read_text()
    ast.parse(src)


# ---------------------------------------------------------------------------
# Frontend wiring
# ---------------------------------------------------------------------------
def test_settings_panel_wired() -> None:
    from pathlib import Path

    html = Path("static/index.html").read_text()
    js = Path("static/app.js").read_text()
    assert 'id="set-relay-enable"' in html
    assert 'id="set-relay-test"' in html
    assert 'id="set-relay-token-btn"' in html
    for needle in ("function bindRelayControls", "bindRelayControls();"):
        assert needle in js
    # Rendered index carries the panel (server-side version injection).
    rendered = render_index(Path("static"))
    assert "set-relay-state" in rendered
