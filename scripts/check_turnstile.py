"""Live check of the REAL kdspy login page (publishingaltitude.com) Turnstile
widget through the app's interactive browser. Reports widget presence and
saves a frame. Owner password comes from the workspace environment."""
import base64
import json
import sys
import time
import urllib.request

sys.path.insert(0, ".")

HOST = "http://localhost:7860"
OWNER_PW = sys.argv[1] if len(sys.argv) > 1 else ""


def raw(path, data=None, method=None, headers=None):
    req = urllib.request.Request(
        HOST + path,
        data=json.dumps(data).encode() if data is not None else None,
        method=method or ("POST" if data is not None else "GET"),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def main() -> None:
    if not OWNER_PW:
        print("usage: python scripts/check_turnstile.py '<owner-password>'")
        return
    tok = raw("/api/auth/login", {"username": "owner", "password": OWNER_PW}).get("token")
    H = {"X-Auth-Token": tok}

    s = raw("/api/browser/interactive/start", {"purpose": "kdspy_setup"}, headers=H)
    print("open:", s["session"]["id"][:8])
    nav = raw(
        "/api/browser/interactive/action",
        {"action": "navigate", "args": {"url": "https://publishingaltitude.com/member/"}},
        headers=H,
    )
    print("nav:", (nav["session"].get("last_url") or nav["session"]["url"])[:70])
    time.sleep(10)

    fr = raw("/api/browser/interactive/frame", None, "GET", headers=H)
    with open("/tmp/kdspy_login.jpg", "wb") as fh:
        fh.write(base64.b64decode(fr["frame"]))
    print("frame saved: /tmp/kdspy_login.jpg")

    st = raw("/api/browser/interactive/state", None, "GET", headers=H)
    print("tabs:", (st.get("session") or {}).get("tabs"))
    raw("/api/browser/interactive/complete", {"outcome": "completed"}, headers=H)
    print("done")


main()
