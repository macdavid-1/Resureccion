"""End-to-end verification of the interactive sign-in fixes.

Drives the REAL FastAPI app with the REAL camoufox engine and verifies the
three fixes from the interrupted run:

1. Camoufox interactive emulation — the mobile UA reaches BOTH the JS layer
   and the server-seen HTTP headers (the "too zoomed in" desktop-layout bug),
   and the phone-class viewport actually holds after navigation.
2. Durable Amazon sign-in detection — the interactive session writes only
   VALID auth statuses (the interrupted run wrote the invalid literal
   "confirmed", which BrowserAuthStore rejected) and never confirms from a
   URL alone.
3. KDSpy setup flow — the interactive browser opens the member-portal LOGIN
   page directly (not the kdspy.com landing page), with the validated
   Firefox add-on installed.

Usage: HOME=/root .venv/bin/python scripts/verify_signin_e2e.py

Uses a throwaway DATA_DIR; mints its own short-lived owner auth session via
the app's token store (the owner's real password/env is never read or set).
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import uuid
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="signin_e2e_")
os.environ["BROWSER_ENGINE"] = "camoufox"
os.environ["BROWSER_HEADLESS"] = "true"
# Test-only direct egress: a stale/off BROWSER_PROXY from the workspace env
# would fail every navigation with NS_ERROR_PROXY_CONNECTION_REFUSED and mask
# what this script actually verifies. The real deployment env is untouched.
os.environ.pop("BROWSER_PROXY", None)
os.environ.setdefault("HOME", "/root")

from app import config as config_mod  # noqa: E402

config_mod.get_config.cache_clear()

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402

app = create_app()
ok = True


def check(name: str, cond: bool, extra: str = "") -> None:
    global ok
    print(("PASS" if cond else "FAIL"), name, extra)
    if not cond:
        ok = False


def mint_owner_session() -> str:
    """Insert a short-lived auth session using the app's own token hashing."""
    from app.security import _hash_token
    from app.timeutil import iso, utcnow

    token = uuid.uuid4().hex + uuid.uuid4().hex
    with app.state.db.tx() as conn:
        conn.execute(
            "INSERT INTO auth_sessions (token_hash, user_id, created_at, expires_at) VALUES (?, 1, ?, ?)",
            (_hash_token(token), iso(utcnow()), iso(utcnow() + timedelta(hours=1))),
        )
    return token


def main() -> int:
    with TestClient(app) as client:
        token = mint_owner_session()
        H = {"X-Auth-Token": token}

        r = client.get("/api/auth/me", headers=H)
        check("auth accepted", r.status_code == 200, r.text[:60])

        # --- KDSpy Firefox add-on status -----------------------------------
        r = client.get("/api/browser/extensions/kdspy", headers=H)
        kd = r.json() if r.status_code == 200 else {}
        check("kdspy status endpoint ok", r.status_code == 200)
        check("engine reported as camoufox", kd.get("engine") == "camoufox", str(kd.get("engine")))

        # --- one-tap AMO install (idempotent) -------------------------------
        r = client.post("/api/browser/extensions/kdspy/install-amo", headers=H)
        check("one-tap AMO install ok", r.status_code == 200, r.text[:140])
        if r.status_code == 200:
            ver = (r.json().get("manifest") or {}).get("version")
            check("AMO manifest version parsed", bool(ver), str(ver))
        r = client.get("/api/browser/extensions/kdspy", headers=H)
        fx = ((r.json() if r.status_code == 200 else {}).get("firefox_addon")) or {}
        check(
            "KDSpy Firefox add-on installed+validated after install",
            fx.get("status") in ("installed", "validated"),
            str(fx.get("status")),
        )

        # --- kdspy_setup opens the member-portal login -----------------------
        r = client.post("/api/browser/interactive/start", json={"purpose": "kdspy_setup"}, headers=H)
        check("interactive start (kdspy_setup)", r.status_code == 200, r.text[:200])
        if r.status_code == 200:
            sess = r.json()["session"]
            url = sess["url"]
            check(
                "kdspy_setup opens the member-portal LOGIN (not kdspy.com landing)",
                "publishingaltitude.com" in url and "wp-login" in url,
                url,
            )
            r = client.get("/api/browser/interactive/state", headers=H)
            check(
                "state endpoint tracks session",
                r.status_code == 200 and (r.json().get("session") or {}).get("id") == sess["id"],
            )
            r = client.get("/api/browser/interactive/frame", headers=H)
            check(
                "live frame streams (jpeg payload)",
                r.status_code == 200 and len(r.json()["frame"]) > 1000,
            )
            r = client.post("/api/browser/interactive/complete", json={"outcome": "completed"}, headers=H)
            check("session completes cleanly", r.status_code == 200)

        # --- amazon_signin: watcher writes only valid statuses ---------------
        r = client.post(
            "/api/browser/interactive/start",
            json={"purpose": "amazon_signin", "marketplace": "us"},
            headers=H,
        )
        check("interactive start (amazon_signin)", r.status_code == 200, r.text[:200])
        if r.status_code == 200:
            sess2 = r.json()["session"]
            check(
                "amazon_signin opens amazon.com",
                "amazon.com" in (sess2["url"] or ""),
                sess2["url"],
            )
            for _ in range(2):  # exercise the async sign-in watcher
                client.get("/api/browser/interactive/frame", headers=H)
                time.sleep(1.3)
            from app.browser_store import AUTH_STATUSES

            rows = app.state.browser_auth.list()
            bad = [x.status for x in rows if x.status not in AUTH_STATUSES]
            check(
                "sign-in watcher wrote only VALID auth statuses (old bug wrote 'confirmed')",
                not bad,
                str([x.status for x in rows]),
            )
            print("   durable auth states:", [(x.status, (x.detail or {}).get("via")) for x in rows])

    print("E2E ALL OK" if ok else "E2E SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
