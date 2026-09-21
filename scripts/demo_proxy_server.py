"""Standalone instrumented Basic-auth CONNECT proxy for live integration checks.

Stands in for a Webshare endpoint: requires Basic auth, logs every CONNECT
with its credentials to stdout, and forwards to the real origin. Run:

    .venv/bin/python scripts/demo_proxy_server.py            # prints port
    .venv/bin/python scripts/demo_proxy_server.py --once     # exit after 1 CONNECT
"""
from __future__ import annotations

import base64
import socket
import sys
import threading

USER = "wsdemo-user"
PASS = "wsdemo-pass"
EXPECTED = "Basic " + base64.b64encode(f"{USER}:{PASS}".encode()).decode()


class Server(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.count = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(16)
        self.port = self._srv.getsockname()[1]

    def run(self) -> None:
        while not self._stop.is_set():
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
                c = conn.recv(65536)
                if not c:
                    conn.close()
                    return
                buf += c
            head, _, body = buf.partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, target, _v = lines[0].split(" ", 2)
            auth = ""
            for ln in lines[1:]:
                if ln.lower().startswith("proxy-authorization:"):
                    auth = ln.split(":", 1)[1].strip()
            if auth != EXPECTED:
                conn.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm=\"p\"\r\nContent-Length: 0\r\n\r\n")
                conn.close()
                return
            creds = base64.b64decode(auth.split(" ", 1)[1]).decode()
            with self._lock:
                self.count += 1
                n = self.count
            print(f"[demo-proxy] CONNECT #{n} {target} auth={creds}", flush=True)
            host, _, port = target.rpartition(":")
            upstream = socket.create_connection((host, int(port)), timeout=15)
            conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            conn.settimeout(None)
            t1 = threading.Thread(target=self._pump, args=(conn, upstream), daemon=True)
            t2 = threading.Thread(target=self._pump, args=(upstream, conn), daemon=True)
            t1.start(); t2.start(); t1.join(); t2.join()
            conn.close()
            upstream.close()
        except Exception:
            try:
                conn.close()
            except Exception:
                pass

    @staticmethod
    def _pump(a: socket.socket, b: socket.socket) -> None:
        try:
            while True:
                d = a.recv(65536)
                if not d:
                    break
                b.sendall(d)
        except Exception:
            pass

    def stop(self) -> None:
        self._stop.set()
        try:
            self._srv.close()
        except Exception:
            pass


if __name__ == "__main__":
    srv = Server()
    srv.start()
    print(f"127.0.0.1:{srv.port}", flush=True)
    try:
        if "--once" in sys.argv:
            import time

            while srv.count < 1:
                time.sleep(0.2)
            srv.stop()
        else:
            import signal

            signal.pause()
    except KeyboardInterrupt:
        pass
