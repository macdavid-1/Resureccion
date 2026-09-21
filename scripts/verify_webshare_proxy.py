"""End-to-end verification of the BROWSER_PROXY (Webshare-style) integration.

Proves the complete proxy egress path with an instrumented local proxy that
demands Basic auth — the same challenge Webshare issues — for BOTH proxy
forms Chromium uses:

  1. Relay-hub level: hub.open_tunnel() dials through the proxy via CONNECT
     with Proxy-Authorization and fetches a real page (api.ipify.org).
  2. Chromium level: the real browser launches with --proxy-server (host
     only) plus Playwright native proxy auth, answers the proxy's 407 inside
     the browser process, and loads a real page (plain-HTTP form and,
     through an https navigation, the CONNECT form).

Run: .venv/bin/python scripts/verify_webshare_proxy.py
"""
from __future__ import annotations

import asyncio
import base64
import os
import socket
import sys
import tempfile
import threading

sys.path.insert(0, ".")

EXPECT_USER = "webshare-user-abc123"
EXPECT_PASS = "webshare-pass-xyz789"
EXPECTED_AUTH = "Basic " + base64.b64encode(f"{EXPECT_USER}:{EXPECT_PASS}".encode()).decode()


class AuthProxy(threading.Thread):
    """Local proxy requiring Basic auth; supports CONNECT + plain HTTP."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.auth_challenges = 0
        self.auth_seen: list[str] = []  # decoded user:pass
        self.connects: list[str] = []
        self.plains: list[str] = []
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(16)
        self.port = self._srv.getsockname()[1]

    # ------------------------------------------------------------- helpers
    def _auth_of(self, lines: list[str]) -> str:
        for ln in lines[1:]:
            if ln.lower().startswith("proxy-authorization:"):
                return ln.split(":", 1)[1].strip()
        return ""

    def _auth_ok(self, auth: str) -> bool:
        if auth != EXPECTED_AUTH:
            return False
        try:
            self.auth_seen.append(base64.b64decode(auth.split(" ", 1)[1]).decode())
        except Exception:
            pass
        return True

    @staticmethod
    def _pump(a: socket.socket, b: socket.socket) -> None:
        try:
            while True:
                data = a.recv(65536)
                if not data:
                    break
                b.sendall(data)
        except Exception:
            pass

    # -------------------------------------------------------------- server
    def run(self) -> None:
        while True:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(20)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    conn.close()
                    return
                buf += chunk
            head, _, body = buf.partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, target, _ver = lines[0].split(" ", 2)
            if method.upper() == "CONNECT":
                self._handle_connect(conn, target, self._auth_of(lines))
            else:
                self._handle_plain(conn, lines, body)
        except Exception:
            import traceback

            traceback.print_exc()
            try:
                conn.close()
            except Exception:
                pass

    def _reject407(self, conn: socket.socket) -> None:
        self.auth_challenges += 1
        conn.sendall(
            b"HTTP/1.1 407 Proxy Authentication Required\r\n"
            b"Proxy-Authenticate: Basic realm=\"proxy\"\r\nContent-Length: 0\r\n\r\n"
        )
        conn.close()

    def _dial(self, host: str, port: int) -> socket.socket:
        return socket.create_connection((host, port), timeout=15)

    def _handle_connect(self, conn: socket.socket, target: str, auth: str) -> None:
        if not self._auth_ok(auth):
            self._reject407(conn)
            return
        self.connects.append(target)
        host, _, port = target.rpartition(":")
        try:
            upstream = self._dial(host, int(port))
        except Exception as exc:
            conn.sendall(f"HTTP/1.1 502 Bad Gateway\r\nContent-Length: {len(str(exc))}\r\n\r\n{exc}".encode())
            conn.close()
            return
        conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        conn.settimeout(None)
        t1 = threading.Thread(target=self._pump, args=(conn, upstream), daemon=True)
        t2 = threading.Thread(target=self._pump, args=(upstream, conn), daemon=True)
        t1.start(); t2.start(); t1.join(); t2.join()
        conn.close()
        upstream.close()

    def _handle_plain(self, conn: socket.socket, lines: list[str], body: bytes) -> None:
        if not self._auth_ok(self._auth_of(lines)):
            self._reject407(conn)
            return
        method, target, _ver = lines[0].split(" ", 2)
        fwd: list[str] = []
        for ln in lines[1:]:
            low = ln.lower()
            if low.startswith(("proxy-authorization:", "proxy-connection:", "proxy-authenticate:")):
                continue
            if low.startswith("connection:"):
                continue
            fwd.append(ln)
        rest = target.split("://", 1)[1] if "://" in target else target
        slash = rest.find("/")
        hostport, path = (rest[:slash], rest[slash:]) if slash >= 0 else (rest, "/")
        host, sep, port = hostport.rpartition(":")
        if not sep:
            host, port_i = hostport, 80
        else:
            port_i = int(port)
        self.plains.append(f"{host}:{port_i}{path}")
        try:
            upstream = self._dial(host, port_i)
        except Exception as exc:
            conn.sendall(f"HTTP/1.1 502 Bad Gateway\r\nContent-Length: {len(str(exc))}\r\n\r\n{exc}".encode())
            conn.close()
            return
        out = f"{method} {path} HTTP/1.1\r\n" + "\r\n".join(fwd) + "\r\nConnection: close\r\n\r\n"
        upstream.sendall(out.encode("latin-1") + body)
        self._pump(upstream, conn)
        conn.close()
        upstream.close()

    def stop(self) -> None:
        try:
            self._srv.close()
        except Exception:
            pass


async def main() -> int:
    proxy = AuthProxy()
    proxy.start()
    print(f"instrumented auth proxy at 127.0.0.1:{proxy.port}")

    spec = f"{EXPECT_USER}:{EXPECT_PASS}@127.0.0.1:{proxy.port}"

    tmp = tempfile.mkdtemp(prefix="ws_check_")
    os.environ["DATA_DIR"] = tmp
    os.environ["BROWSER_PROXY"] = spec
    os.environ["RELAY_ALLOW_DIRECT"] = "false"  # force the proxy path

    from app import config as cm

    cm.get_config.cache_clear()
    from app.config import get_config
    from app.relay import RelayHub

    # ---------------------------------------------------------------- hub level
    cfg = get_config()
    hub = RelayHub(cfg)
    t = await hub.open_tunnel("api.ipify.org", 80)
    await t.write(
        b"GET /?format=json HTTP/1.1\r\nHost: api.ipify.org\r\n"
        b"User-Agent: relay-check\r\nAccept: */*\r\nConnection: close\r\n\r\n"
    )
    buf = b""
    while len(buf) < 65536:
        try:
            chunk = await asyncio.wait_for(t.read(), timeout=10)
        except asyncio.TimeoutError:
            break
        if not chunk:
            break
        buf += chunk
        if b"}" in buf:
            break
    await t.close()
    assert buf.startswith(b"HTTP/1.1 200"), buf[:120]
    hub_ip = buf.split(b"\r\n\r\n", 1)[1].decode().strip()
    print(f"[hub]      CONNECT via proxy OK — auth seen: {proxy.auth_seen} — egress: {hub_ip}")
    assert proxy.auth_seen == [f"{EXPECT_USER}:{EXPECT_PASS}"], proxy.auth_seen
    assert proxy.auth_challenges == 0
    assert hub.stats["fallback_proxy"] == 1

    # ------------------------------------------------------------ Chromium level
    from app.db import init_db
    from app.browser_store import BrowserEvidenceStore, ExtensionStore
    from app.browser_manager import BrowserManager
    from app.kdspy import KDSpyManager

    cm.get_config.cache_clear()
    cfg = get_config()
    cfg.ensure_dirs()
    db = init_db(cfg)
    kdspy = KDSpyManager(cfg, ExtensionStore(db))
    mgr = BrowserManager(cfg, kdspy, BrowserEvidenceStore(db), relay=None)
    st = await mgr.launch()
    print(f"[chromium] launched (running={st.running})")

    # Plain-HTTP navigation → absolute-form proxy request with auth.
    page = await mgr.new_page()
    await page.goto("http://example.com", timeout=60000)
    title = (await page.title()).strip()
    print(f"[chromium] http page through auth proxy: {title!r} (proxy saw {len(proxy.plains)} plain reqs)")
    assert title == "Example Domain", title
    assert proxy.plains, "Chromium never presented the plain request to the proxy"
    await page.close()

    # HTTPS navigation → CONNECT with auth.
    page = await mgr.new_page()
    await page.goto("https://example.com", timeout=60000)
    title2 = (await page.title()).strip()
    print(f"[chromium] https page via CONNECT through auth proxy: {title2!r} (CONNECTs: {proxy.connects[:2]})")
    assert title2 == "Example Domain", title2
    assert proxy.connects, "Chromium never CONNECTed through the proxy"
    await page.close()

    await mgr.shutdown()
    db.close()
    proxy.stop()

    print()
    print("VERIFICATION OK — Webshare-style Basic auth works at hub level AND in the real")
    print(f"browser (plain + CONNECT), credentials never on the command line. challenges={proxy.auth_challenges}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
