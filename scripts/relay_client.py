#!/usr/bin/env python3
"""Resurrección owner-device relay client.

Run this on the owner's phone (Termux) or any always-on home device
(Raspberry Pi, old laptop). It dials OUT to the Resurrección server — no
port forwarding, works on mobile carrier networks — and shuttles the
research browser's TCP bytes so every request exits through THIS device's
IP (trusted mobile/residential). TLS stays end-to-end between Chromium and
the sites; the device relays only opaque encrypted bytes and destination
hostnames, and can never read page content or research data.

Setup (phone / Termux):
    pkg install python && pip install -U pip
    python relay_client.py --server https://<your-space>.hf.space --token <relay token>

Setup (home device):
    python3 relay_client.py --server http://<server>:7860 --token <relay token>

The token comes from Resurrección → Settings → Device Relay → Show token.
Keep this process running (Termux: acquire wakelock, disable battery
optimization for Termux) so research always has a trusted exit available:

    termux-wake-lock

The client is idempotent to reconnects: if the connection drops it retries
with backoff forever, so the device can sleep, roam, or change networks.
"""

from __future__ import annotations

import argparse
import base64
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

MAX_SOCKET_BUF = 64 * 1024


def _http_json(url: str, payload: dict | None = None, token: str = "", timeout: float = 30) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method="POST" if data is not None else "GET",
        headers={
            "Content-Type": "application/json",
            **({"X-Relay-Token": token} if token else {}),
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _dial(host: str, port: int, timeout: float = 20) -> socket.socket:
    last_exc: Exception | None = None
    for fam, _, _, _, sa in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        try:
            s = socket.socket(fam, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect(sa)
            s.settimeout(None)
            return s
        except OSError as exc:
            last_exc = exc
            try:
                s.close()
            except Exception:
                pass
    raise OSError(f"cannot dial {host}:{port}: {last_exc}")


class Conn:
    __slots__ = ("conn_id", "host", "port", "sock", "lock", "closed")

    def __init__(self, conn_id: int, host: str, port: int) -> None:
        self.conn_id = conn_id
        self.host = host
        self.port = port
        self.sock: socket.socket | None = None
        self.lock = threading.Lock()
        self.closed = False

    def open(self) -> None:
        self.sock = _dial(self.host, self.port)

    def send_upstream(self, server: str, token: str, payload: dict) -> None:
        try:
            _http_json(server + "/api/relay/upstream", payload, token)
        except Exception:
            pass

    def pump(self, server: str, token: str) -> None:
        """Socket → upstream POSTs until EOF/error."""
        try:
            while not self.closed:
                chunk = self.sock.recv(MAX_SOCKET_BUF)
                if not chunk:
                    self.send_upstream(server, token, {"messages": [{"op": "closed", "conn": self.conn_id}]})
                    break
                self.send_upstream(
                    server, token,
                    {"messages": [{"op": "data", "conn": self.conn_id, "b64": base64.b64encode(chunk).decode()}]},
                )
        except Exception:
            self.send_upstream(server, token, {"messages": [{"op": "closed", "conn": self.conn_id}]})
        finally:
            self.closed = True

    def write(self, data: bytes) -> None:
        if self.sock is None or self.closed:
            return
        try:
            self.sock.sendall(data)
        except Exception:
            self.closed = True

    def close(self) -> None:
        self.closed = True
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass


def run_downstream(server: str, token: str) -> None:
    """SSE reader: consumes hub → device commands forever (with backoff)."""
    backoff = 1.0
    conns: dict[int, Conn] = {}
    upstream_buf: list[dict] = []
    buf_lock = threading.Lock()
    stop = threading.Event()

    def upstream_worker() -> None:
        while not stop.is_set():
            with buf_lock:
                batch, upstream_buf[:] = upstream_buf[:64], upstream_buf[64:]
            if batch:
                try:
                    _http_json(server + "/api/relay/upstream", {"messages": batch}, token)
                except Exception:
                    with buf_lock:
                        upstream_buf[:0] = batch  # retry later
                    time.sleep(0.5)
            else:
                time.sleep(0.05)

    threading.Thread(target=upstream_worker, daemon=True).start()

    while not stop.is_set():
        try:
            req = urllib.request.Request(
                server + "/api/relay/stream",
                headers={"X-Relay-Token": token, "Accept": "text/event-stream"},
            )
            resp = urllib.request.urlopen(req, timeout=65)
            print(f"[relay] paired — shuttling browser traffic for {server}", flush=True)
            backoff = 1.0
            event_lines: list[str] = []
            while True:
                line = resp.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").rstrip("\r\n")
                if text == "":
                    if event_lines:
                        _handle_event(event_lines, conns, server, token, buf_lock, upstream_buf)
                        event_lines = []
                    continue
                if text.startswith("data: "):
                    event_lines.append(text[6:])
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f"[relay] disconnected ({exc}); retrying in {backoff:.0f}s", flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
        finally:
            for c in conns.values():
                c.close()
            conns.clear()


def _handle_event(lines: list[str], conns: dict[int, Conn], server: str, token: str, buf_lock, upstream_buf: list) -> None:
    try:
        msg = json.loads("\n".join(lines))
    except Exception:
        return
    op = msg.get("op")
    if op == "open":
        conn_id = msg.get("conn")
        host, port = msg.get("host") or "", int(msg.get("port") or 0)
        c = Conn(conn_id, host, port)
        try:
            c.open()
            conns[conn_id] = c
            upstream_buf.append({"op": "opened", "conn": conn_id})
            threading.Thread(target=c.pump, args=(server, token), daemon=True).start()
        except Exception as exc:
            upstream_buf.append({"op": "open_error", "conn": conn_id, "error": str(exc)[:200]})
    elif op == "data":
        c = conns.get(msg.get("conn"))
        if c:
            try:
                c.write(base64.b64decode(msg.get("b64") or ""))
            except Exception:
                pass
    elif op == "close":
        c = conns.pop(msg.get("conn"), None)
        if c:
            c.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Resurrección owner-device relay client")
    ap.add_argument("--server", required=True, help="e.g. https://<space>.hf.space or http://host:7860")
    ap.add_argument("--token", required=True, help="relay token from Settings → Device Relay")
    ap.add_argument("--once-through", action="store_true", help="exit after the first disconnect (for supervision)")
    args = ap.parse_args()
    server = args.server.rstrip("/")
    if not server.startswith(("http://", "https://")):
        print("server must start with http:// or https://", file=sys.stderr)
        return 2
    if args.once_through:
        run_downstream(server, args.token)
        return 0
    # Default: supervised forever.
    try:
        run_downstream(server, args.token)
    except KeyboardInterrupt:
        print("[relay] stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
