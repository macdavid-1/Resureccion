"""Owner-device egress relay ("phone IP" mode).

The research browser normally exits from the server's IP — a datacenter
address that anti-bot systems (reCAPTCHA, Cloudflare Turnstile, Amazon
walls) distrust. This module lets every browser request exit through the
OWNER's own device instead, whose mobile/carrier/home IP is a trusted,
residential-class address.

Architecture (reverse relay — works through any NAT/firewall):

    Chromium ── HTTP/CONNECT proxy ──> local shim (127.0.0.1)
        shim ── control messages ──> RelayHub (inside the app)
        hub   ── SSE stream ───────> owner's device (outbound-only connect)
        owner's device ── TCP to origin ──> marketplace
        owner's device ── POST /api/relay/upstream ──> hub ──> shim ──> Chromium

The device dials OUT to the server (no port forwarding, works on cellular),
shuttles raw TCP bytes, and the origin site sees the device's IP. Requests
are tunneled opaquely (TLS stays end-to-end between Chromium and the site —
the relay sees only encrypted bytes and destination hostnames).

Fallbacks (per connection, in order): owner device → external proxy
(``BROWSER_PROXY`` — HTTP CONNECT or SOCKS5, per the spec's scheme) →
direct from the server (if allowed).
This keeps long research runs alive when the device sleeps, while using
the device's IP whenever it is connected. The current egress is always
visible in Settings, and an exit-IP test shows exactly what sites see.

Privacy: the shim refuses tracker/ad hosts (see app/privacy.py) BEFORE any
byte reaches the device network, WebRTC UDP bypass is disabled at launch,
and DNT/GPC preferences are declared. The tunnel protocol cannot carry
research data anywhere: it only pipes bytes between Chromium and the
device the owner paired.

Control-plane security: stream/upstream endpoints require the relay token
(bearer secret, purpose-scoped to byte shuttling; cannot read research
data). Status/pair/test endpoints require full owner auth.
"""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.privacy import blocked_check
from app.proxy_spec import ProxySpecError, proxy_parts
from app.timeutil import iso_now

_CHUNK = 64 * 1024  # tunnel frame size (bytes before base64)
_OPEN_TIMEOUT = 20.0  # seconds for a device to ack an open
_PHONE_STALE_SECONDS = 45.0  # SSE silence after which the device counts as gone
_MAX_HEAD = 32 * 1024
_MAX_PLAIN_BODY = 10 * 1024 * 1024


class RelayError(Exception):
    pass


class TunnelError(Exception):
    """A tunnel could not be established or died mid-flight."""


# ---------------------------------------------------------------------------
# Tunnels
# ---------------------------------------------------------------------------
@dataclass
class Tunnel:
    """One TCP stream to an origin, carried over the relay or a fallback."""

    conn_id: int
    hub: "RelayHub"
    opened: asyncio.Event = field(default_factory=asyncio.Event)
    open_error: str | None = None
    inbound: asyncio.Queue = field(default_factory=asyncio.Queue)  # bytes chunks
    closed: bool = False
    _eof_sentinel: bool = False

    async def write(self, data: bytes) -> None:
        if self.closed:
            raise TunnelError("tunnel closed")
        if not data:
            return
        await self.hub._to_device(
            {"op": "data", "conn": self.conn_id, "b64": base64.b64encode(data).decode("ascii")}
        )

    async def read(self) -> bytes:
        """Next chunk from the origin; b"" at clean EOF. Raises on reset."""
        if self._eof_sentinel:
            return b""
        item = await self.inbound.get()
        if item is None:  # closed sentinel
            self._eof_sentinel = True
            return b""
        return item  # type: ignore[return-value]

    def device_closed(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                self.inbound.put_nowait(None)
            except Exception:
                pass

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                self.inbound.put_nowait(None)
            except Exception:
                pass
            try:
                await self.hub._to_device({"op": "close", "conn": self.conn_id})
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Relay hub
# ---------------------------------------------------------------------------
class RelayHub:
    """Pairs with the owner's device and brokers tunnels for the shim."""

    def __init__(self, config: Any) -> None:
        self.config = config
        self.enabled = False
        self._device_queue: asyncio.Queue | None = None  # SSE downstream queue
        self._last_device_seen = 0.0
        self._next_conn = 1
        self._conns: dict[int, Tunnel] = {}
        self._pending_open: dict[int, Tunnel] = {}
        self.stats = {
            "connections_total": 0,
            "bytes_to_device": 0,
            "bytes_from_device": 0,
            "fallback_direct": 0,
            "fallback_proxy": 0,
            "blocked_requests": 0,
            "relay_served": 0,
        }
        self.shim: "ShimServer | None" = None
        self._token_cache: str | None = None

    # ------------------------------------------------------------- token
    @property
    def token_path(self) -> Path:
        return Path(self.config.data_dir) / "relay_token"

    def token(self) -> str:
        """Relay bearer token: env override, else generated once and stored
        atomically under DATA_DIR with owner-only permissions."""
        if self._token_cache:
            return self._token_cache
        env = getattr(self.config, "relay_token", "") or ""
        if env:
            self._token_cache = env
            return env
        p = self.token_path
        if p.exists():
            tok = p.read_text(encoding="utf-8").strip()
            if tok:
                self._token_cache = tok
                return tok
        tok = "rr_" + secrets.token_urlsafe(32)
        from app.db import atomic_write_text

        atomic_write_text(p, tok + "\n")
        try:
            import os

            os.chmod(p, 0o600)
        except Exception:
            pass
        self._token_cache = tok
        return tok

    def rotate_token(self) -> str:
        self._token_cache = "rr_" + secrets.token_urlsafe(32)
        from app.db import atomic_write_text

        atomic_write_text(self.token_path, self._token_cache + "\n")
        try:
            import os

            os.chmod(self.token_path, 0o600)
        except Exception:
            pass
        return self._token_cache  # type: ignore[return-value]

    def check_token(self, provided: str | None) -> bool:
        if not provided:
            return False
        import hmac

        return hmac.compare_digest(provided.strip(), self.token())

    # ------------------------------------------------------------ device
    @property
    def device_connected(self) -> bool:
        return (
            self._device_queue is not None
            and (time.monotonic() - self._last_device_seen) < _PHONE_STALE_SECONDS
        )

    def device_last_seen_iso(self) -> str | None:
        if self._last_device_seen == 0.0:
            return None
        import datetime

        return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=time.monotonic() - self._last_device_seen)).isoformat()

    def attach_device(self) -> asyncio.Queue:
        """SSE handler: register the device and return its downstream queue."""
        self._device_queue = asyncio.Queue(maxsize=4096)
        self._last_device_seen = time.monotonic()
        return self._device_queue

    def detach_device(self, q: asyncio.Queue) -> None:
        if self._device_queue is q:
            self._device_queue = None
            # The tunnel carrier is gone: fail all live tunnels fast so the
            # shim resets and Chromium retries on the next path.
            for t in list(self._conns.values()):
                t.device_closed()
            self._conns.clear()

    def touch_device(self) -> None:
        self._last_device_seen = time.monotonic()

    async def _to_device(self, msg: dict[str, Any]) -> None:
        q = self._device_queue
        if q is None or not self.device_connected:
            raise TunnelError("owner device not connected")
        if msg.get("b64"):
            self.stats["bytes_to_device"] += len(msg["b64"]) * 3 // 4
        await q.put(json.dumps(msg, separators=(",", ":")))

    async def upstream(self, messages: list[dict[str, Any]]) -> int:
        """Device → hub messages (open acks, data, closes)."""
        self.touch_device()
        handled = 0
        for m in messages:
            op = m.get("op")
            conn_id = m.get("conn")
            if op == "opened" and isinstance(conn_id, int):
                t = self._pending_open.pop(conn_id, None)
                if t:
                    self._conns[conn_id] = t
                    t.opened.set()
                handled += 1
            elif op == "open_error" and isinstance(conn_id, int):
                t = self._pending_open.pop(conn_id, None)
                if t:
                    t.open_error = str(m.get("error") or "device could not dial target")
                    t.opened.set()
                handled += 1
            elif op == "data" and isinstance(conn_id, int):
                t = self._conns.get(conn_id)
                if t is not None and not t.closed:
                    try:
                        raw = base64.b64decode(m.get("b64") or "", validate=True)
                    except Exception:
                        continue
                    self.stats["bytes_from_device"] += len(raw)
                    await t.inbound.put(raw)
                    handled += 1
            elif op == "closed" and isinstance(conn_id, int):
                t = self._conns.pop(conn_id, None)
                if t:
                    t.device_closed()
                handled += 1
        return handled

    # ----------------------------------------------------------- tunnels
    async def open_tunnel(self, host: str, port: int) -> Tunnel:
        """Open a TCP stream to host:port — via the device when connected,
        else external proxy, else direct (per config)."""
        host = (host or "").strip().lower()
        reason = blocked_check(host)
        if reason:
            self.stats["blocked_requests"] += 1
            raise TunnelError(reason)
        self.stats["connections_total"] += 1

        if self.device_connected:
            conn_id = self._next_conn
            self._next_conn += 1
            t = Tunnel(conn_id=conn_id, hub=self)
            self._pending_open[conn_id] = t
            try:
                await self._to_device({"op": "open", "conn": conn_id, "host": host, "port": int(port)})
                await asyncio.wait_for(t.opened.wait(), timeout=_OPEN_TIMEOUT)
            except asyncio.TimeoutError:
                self._pending_open.pop(conn_id, None)
                t.closed = True
                raise TunnelError(f"device did not open {host}:{port} in time") from None
            except TunnelError:
                self._pending_open.pop(conn_id, None)
                t.closed = True
                raise
            if t.open_error:
                t.closed = True
                # Device refused (offline network etc.) → fall through to fallbacks.
            else:
                self.stats["relay_served"] += 1
                return t

        # --- fallback: external proxy (CONNECT), then direct ---------------
        proxy = (getattr(self.config, "browser_proxy", "") or "").strip()
        if proxy:
            try:
                reader, writer = await _dial_via_proxy(proxy, host, int(port))
                self.stats["fallback_proxy"] += 1
                return await self._direct_tunnel(reader, writer)
            except Exception:
                pass
        if getattr(self.config, "relay_allow_direct", True):
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, int(port)), timeout=15
                )
                self.stats["fallback_direct"] += 1
                return await self._direct_tunnel(reader, writer)
            except Exception as exc:
                raise TunnelError(f"all egress paths failed for {host}:{port}: {exc}") from exc
        raise TunnelError(
            "no egress available: owner device offline and direct fallback disabled"
        )

    async def _direct_tunnel(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> Tunnel:
        """Wrap a real socket pair as a Tunnel (fallback path)."""
        conn_id = self._next_conn
        self._next_conn += 1
        t = Tunnel(conn_id=conn_id, hub=self)
        t.opened.set()
        self._conns[conn_id] = t

        async def _pump() -> None:
            try:
                while not t.closed:
                    chunk = await reader.read(_CHUNK)
                    if not chunk:
                        break
                    await t.inbound.put(chunk)
            except Exception:
                pass
            finally:
                t.device_closed()
                self._conns.pop(conn_id, None)
                try:
                    writer.close()
                except Exception:
                    pass

        asyncio.get_running_loop().create_task(_pump())

        # Device writes: Tunnel.write must forward to the socket.
        async def _write(data: bytes) -> None:
            writer.write(data)
            await writer.drain()

        t.write = _write  # type: ignore[method-assign]
        return t

    # ------------------------------------------------------------ status
    def status(self) -> dict[str, Any]:
        shim_addr = None
        if self.shim and self.shim.port:
            shim_addr = f"127.0.0.1:{self.shim.port}"
        tok = self.token()
        return {
            "enabled": self.enabled,
            "shim": shim_addr,
            "device_connected": self.device_connected,
            "device_last_seen": self.device_last_seen_iso(),
            "token_fingerprint": tok[:6] + "…" if len(tok) > 6 else "…",
            "mode": getattr(self.config, "relay_mode", "phone_first"),
            "allow_direct": getattr(self.config, "relay_allow_direct", True),
            "stats": dict(self.stats),
            "now": iso_now(),
        }

    # ------------------------------------------------------------- enable
    async def start(self) -> dict[str, Any]:
        if self.enabled and self.shim:
            return self.status()
        self.shim = ShimServer(self)
        await self.shim.start()
        self.enabled = True
        return self.status()

    async def stop(self) -> None:
        self.enabled = False
        if self.shim:
            await self.shim.stop()
            self.shim = None


# ---------------------------------------------------------------------------
# Fallback dialing
# ---------------------------------------------------------------------------
async def _dial_via_proxy(proxy: str, host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Tunnel through the external BROWSER_PROXY.

    Scheme-aware: ``socks5://`` (e.g. a Cloudflare WARP bridge on the
    owner's VPS) speaks the RFC 1928 handshake; everything else uses HTTP
    CONNECT. Credentials come from the spec (never from anywhere else).
    """
    try:
        scheme, phost, pport, username, password = proxy_parts(proxy)
    except ProxySpecError as exc:
        raise TunnelError(f"invalid BROWSER_PROXY: {exc}") from exc
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(phost, pport), timeout=15
        )
    except Exception as exc:
        raise TunnelError(f"cannot reach proxy {phost}:{pport}: {exc}") from exc
    if scheme in ("socks5", "socks5h"):
        try:
            await asyncio.wait_for(
                _socks5_connect(reader, writer, host, int(port), username, password),
                timeout=25,
            )
        except TunnelError:
            writer.close()
            raise
        except Exception as exc:
            writer.close()
            raise TunnelError(f"SOCKS5 tunnel failed: {exc}") from exc
        return reader, writer
    # --- HTTP CONNECT -----------------------------------------------------
    lines = [f"CONNECT {host}:{port} HTTP/1.1", f"Host: {host}:{port}"]
    if username:
        import base64 as b64mod

        cred = b64mod.b64encode(f"{username}:{password or ''}".encode()).decode("ascii")
        lines.append(f"Proxy-Authorization: Basic {cred}")
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
    await writer.drain()
    try:
        status_line = await asyncio.wait_for(reader.readline(), timeout=20)
    except asyncio.TimeoutError as exc:
        writer.close()
        raise TunnelError("external proxy did not answer CONNECT in time") from exc
    if b" 200" not in status_line:
        writer.close()
        raise TunnelError(f"external proxy refused CONNECT: {status_line[:80]!r}")
    # Drain proxy response headers.
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
    return reader, writer


_SOCKS5_ERRORS = {
    1: "general failure",
    2: "connection not allowed by ruleset",
    3: "network unreachable",
    4: "host unreachable",
    5: "connection refused",
    6: "TTL expired",
    7: "command not supported",
    8: "address type not supported",
}


async def _socks5_connect(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    host: str,
    port: int,
    username: str | None,
    password: str | None,
) -> None:
    """RFC 1928 client handshake, then CONNECT host:port.

    The hostname is sent as ATYP=domain so the proxy resolves DNS (the
    privacy-preserving choice: the server never learns which hosts the
    browser asked about when the bridge resolves them).
    """
    import asyncio as _asyncio

    methods = b"\x02\x00" if username else b"\x00"
    writer.write(b"\x05" + bytes([len(methods)]) + methods)
    await writer.drain()
    sel = await _asyncio.wait_for(reader.readexactly(2), timeout=15)
    if sel[0] != 0x05:
        raise TunnelError(f"not a SOCKS5 proxy (got version byte {sel[0]:#x})")
    if sel[1] == 0x02:
        if not username:
            raise TunnelError("SOCKS5 proxy demands authentication but BROWSER_PROXY has no credentials")
        u = username.encode("utf-8")
        p = (password or "").encode("utf-8")
        if len(u) > 255 or len(p) > 255:
            raise TunnelError("SOCKS5 credentials too long (RFC 1928 caps at 255 bytes)")
        writer.write(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
        await writer.drain()
        sub = await _asyncio.wait_for(reader.readexactly(2), timeout=15)
        if sub[0] != 0x01 or sub[1] != 0x00:
            raise TunnelError("SOCKS5 authentication failed (check user:pass)")
    elif sel[1] != 0x00:
        raise TunnelError("SOCKS5 proxy rejected the offered auth methods")
    host_b = host.encode("utf-8")
    if len(host_b) > 255:
        raise TunnelError("target hostname too long for SOCKS5")
    writer.write(
        b"\x05\x01\x00\x03" + bytes([len(host_b)]) + host_b + int(port).to_bytes(2, "big")
    )
    await writer.drain()
    resp = await _asyncio.wait_for(reader.readexactly(4), timeout=20)
    if resp[0] != 0x05:
        raise TunnelError("malformed SOCKS5 reply")
    if resp[1] != 0x00:
        raise TunnelError(
            f"SOCKS5 CONNECT refused: {_SOCKS5_ERRORS.get(resp[1], f'code {resp[1]}')}"
        )
    atyp = resp[3]
    if atyp == 0x01:
        await _asyncio.wait_for(reader.readexactly(4), timeout=15)
    elif atyp == 0x03:
        n = (await _asyncio.wait_for(reader.readexactly(1), timeout=15))[0]
        await _asyncio.wait_for(reader.readexactly(n), timeout=15)
    elif atyp == 0x04:
        await _asyncio.wait_for(reader.readexactly(16), timeout=15)
    else:
        raise TunnelError(f"unknown SOCKS5 address type {atyp:#x}")
    await _asyncio.wait_for(reader.readexactly(2), timeout=15)  # bound port


# ---------------------------------------------------------------------------
# Local proxy shim (what Chromium points at)
# ---------------------------------------------------------------------------
class ShimServer:
    """127.0.0.1-only HTTP+CONNECT proxy that tunnels through the hub."""

    def __init__(self, hub: RelayHub) -> None:
        self.hub = hub
        self.server: asyncio.AbstractServer | None = None
        self.port: int | None = None

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]  # type: ignore[index]

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            try:
                await self.server.wait_closed()
            except Exception:
                pass
        self.server = None
        self.port = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=30)
        except Exception:
            writer.close()
            return
        if len(head) > _MAX_HEAD:
            writer.close()
            return
        try:
            lines = head.decode("latin-1").split("\r\n")
            method, target, _ver = lines[0].split(" ", 2)
        except Exception:
            writer.close()
            return
        if method.upper() == "CONNECT":
            await self._handle_connect(reader, writer, target)
        else:
            await self._handle_plain(reader, writer, method.upper(), target, lines[1:])

    # ------------------------------------------------------------- CONNECT
    async def _handle_connect(self, reader, writer, target: str) -> None:
        host, _, port_s = target.rpartition(":")
        try:
            port = int(port_s)
        except ValueError:
            writer.close()
            return
        reason = blocked_check(host)
        if reason:
            self.hub.stats["blocked_requests"] += 1
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        try:
            tunnel = await self.hub.open_tunnel(host, port)
        except TunnelError as exc:
            writer.write(
                f"HTTP/1.1 502 Bad Gateway\r\nContent-Length: {len(str(exc))}\r\nConnection: close\r\n\r\n{exc}".encode()
            )
            await writer.drain()
            writer.close()
            return
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await self._shuttle(reader, writer, tunnel)

    async def _shuttle(self, reader, writer, tunnel: Tunnel) -> None:
        """Bidirectional raw-byte pump between Chromium and a tunnel."""

        async def c2t() -> None:
            try:
                while not tunnel.closed:
                    data = await reader.read(_CHUNK)
                    if not data:
                        break
                    await tunnel.write(data)
            except Exception:
                pass
            finally:
                await tunnel.close()

        async def t2c() -> None:
            try:
                while True:
                    data = await tunnel.read()
                    if not data:
                        break
                    writer.write(data)
                    await writer.drain()
            except Exception:
                pass
            finally:
                try:
                    writer.close()
                except Exception:
                    pass

        await asyncio.gather(c2t(), t2c(), return_exceptions=True)
        try:
            writer.close()
        except Exception:
            pass

    # ---------------------------------------------------------- plain HTTP
    async def _handle_plain(self, reader, writer, method: str, target: str, headers: list[str]) -> None:
        """Absolute-form HTTP/1.x proxy request → tunneled origin-form request."""
        hdrs: dict[str, str] = {}
        for h in headers:
            if ":" in h:
                k, _, v = h.partition(":")
                hdrs[k.strip().lower()] = v.strip()
        host_hdr = hdrs.get("host", "")
        host = host_hdr.split(":")[0].strip()
        if not host:
            writer.close()
            return
        reason = blocked_check(host)
        if reason:
            self.hub.stats["blocked_requests"] += 1
            body = b"blocked by privacy filter\n"
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n")
            writer.write(body)
            await writer.drain()
            writer.close()
            return
        # Rewrite absolute-form → origin-form for the origin server.
        path = target
        for scheme in ("http://", "https://"):
            if target.lower().startswith(scheme):
                rest = target[len(scheme):]
                slash = rest.find("/")
                path = rest[slash:] if slash >= 0 else "/"
                break
        # Body (Content-Length only; Chromium does not use chunked requests).
        body = b""
        clen = int(hdrs.get("content-length", "0") or 0)
        if clen:
            if clen > _MAX_PLAIN_BODY:
                writer.close()
                return
            body = await reader.readexactly(clen)
        hop_by_hop = {
            "connection", "keep-alive", "proxy-authorization", "proxy-authenticate",
            "proxy-connection", "te", "trailers", "upgrade",
        }
        out = [f"{method} {path} HTTP/1.1", f"Host: {host_hdr}"]
        for h in headers:
            if ":" not in h:
                continue
            k, _, v = h.partition(":")
            if k.strip().lower() in hop_by_hop or k.strip().lower() == "host":
                continue
            out.append(h.strip())
        out.append("Connection: close")
        blob = ("\r\n".join(out) + "\r\n\r\n").encode("latin-1") + body
        port = int(host_hdr.rpartition(":")[2]) if host_hdr.count(":") >= 1 and host_hdr.rpartition(":")[2].isdigit() else 80
        try:
            tunnel = await self.hub.open_tunnel(host, port)
        except TunnelError as exc:
            msg = str(exc).encode()
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n")
            writer.write(msg)
            await writer.drain()
            writer.close()
            return
        try:
            await tunnel.write(blob)
            # Stream the response back incrementally.
            while True:
                chunk = await tunnel.read()
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
        except Exception:
            pass
        finally:
            await tunnel.close()
            try:
                writer.close()
            except Exception:
                pass
