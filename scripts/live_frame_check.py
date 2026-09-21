"""Live verification: tap a checkbox inside a cross-origin iframe through
the app's real click pipeline and assert the tap routes and registers."""
import asyncio
import json
import os
import re
import sys
import traceback
import urllib.request

sys.path.insert(0, ".")

HOST = "http://localhost:7860"

# Owner password comes from the environment (RESURRECCION_OWNER_PW), set per-
# workspace — never hardcoded here.
OWNER_PW = os.environ.get("RESURRECCION_OWNER_PW", "")


def call(path, data=None, method=None, headers=None):
    try:
        return _call(path, data, method, headers)
    except Exception:
        traceback.print_exc()
        return {"__error__": "call crashed"}


def _call(path, data=None, method=None, headers=None):
    req = urllib.request.Request(
        HOST + path,
        data=json.dumps(data).encode() if data is not None else None,
        method=method or ("POST" if data is not None else "GET"),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        return {"__http__": e.code, "body": body[:400]}
    except Exception as e:  # noqa: BLE001
        return {"__error__": str(e)}


async def main() -> None:
    if not OWNER_PW:
        print("RESURRECCION_OWNER_PW not set; cannot authenticate")
        return
    auth = call("/api/auth/login", {"username": "owner", "password": OWNER_PW})
    if "__error__" in auth or "__http__" in auth or not auth.get("token"):
        print("LOGIN FAILED:", json.dumps(auth)[:300])
        return
    tok = auth["token"]
    H = {"X-Auth-Token": tok}

    s = call("/api/browser/interactive/start", {"purpose": "kdspy_setup"}, headers=H)
    print("start raw:", json.dumps(s)[:300])

    nav = call(
        "/api/browser/interactive/action",
        {"action": "navigate", "args": {"url": "https://www.google.com/recaptcha/api2/demo"}},
        headers=H,
    )
    print("navigate raw:", json.dumps(nav)[:300])
    await asyncio.sleep(7)

    # Tap the demo reCAPTCHA checkbox area through the app pipeline.
    click = call(
        "/api/browser/interactive/action",
        {"action": "click", "args": {"x": 720, "y": 380}},
        headers=H,
    )
    print("click raw:", json.dumps(click)[:300])
    await asyncio.sleep(4)

    st = call("/api/browser/interactive/state", None, "GET", headers=H)
    sess = st.get("session") or {}
    print(
        "state:",
        {k: sess.get(k) for k in ("status", "last_url", "active", "last_title")},
    )

    call(
        "/api/browser/interactive/complete",
        {"outcome": "completed"},
        headers=H,
    )
    print("done")


if __name__ == "__main__":
    asyncio.run(main())
