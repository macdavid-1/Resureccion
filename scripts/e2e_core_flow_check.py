"""End-to-end check of the core owner flow, driven over the real HTTP API.

Reproduces the preview-server environment (HOME=/root — the account WITHOUT
the camoufox download) and then exercises, in order, exactly what the UI does:

  1. login (single-owner auth)
  2. POST /api/browser/extensions/kdspy/install-amo   (Settings button → AMO)
  3. POST /api/browser/launch                          (camoufox engine)
  4. POST /api/browser/interactive/start kdspy_setup   (sign-in browser)
  5. GET  /api/browser/interactive/frame               (rendered page)
  6. identity coherence + add-on-active assertions inside the live browser

Run: .venv/bin/python scripts/e2e_core_flow_check.py
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PY = sys.executable


# --------------------------------------------------------------------- child
def run_e2e() -> int:
    import httpx  # noqa: F401  (ensure installed before app imports)

    # Isolated runtime data + owner creds BEFORE any app import.
    tmp = Path(tempfile.mkdtemp(prefix="e2e_core_"))
    os.environ["DATA_DIR"] = str(tmp)
    os.environ["OWNER_USERNAME"] = "owner"
    os.environ["BROWSER_ENGINE"] = "camoufox"
    os.environ["BROWSER_HEADLESS"] = "true"
    from app.security import hash_password

    os.environ["OWNER_PASSWORD_HASH"] = hash_password("e2e-owner-pass")
    os.environ["AUTH_SECRET"] = "e2e-auth-secret"

    from app import config as config_mod

    config_mod.get_config.cache_clear()

    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app()
    steps: list[str] = []

    def ok(label: str, detail: str = "") -> None:
        steps.append(label)
        print(f"PASS {label}" + (f" — {detail}" if detail else ""))

    with TestClient(app) as c:
        # 1. -------------------------------------------------------- login
        r = c.post(
            "/api/auth/login", json={"username": "owner", "password": "e2e-owner-pass"}
        )
        assert r.status_code == 200, r.text
        headers = {"X-Auth-Token": r.json()["token"]}
        ok("1 login (owner auth)")

        # 2. --------------------------------- install KDSpy add-on from AMO
        r = c.post("/api/browser/extensions/kdspy/install-amo", headers=headers)
        assert r.status_code == 200, r.text
        data = r.json()
        manifest = data["manifest"]
        print(
            "     AMO manifest:",
            json.dumps(manifest, ensure_ascii=False)[:120],
        )
        assert "kdspy" in manifest["name"].lower(), manifest
        assert data["firefox_addon"]["status"] in ("installed", "validated"), data
        ok(
            "2 install-amo (UI route)",
            f"v{manifest['version']} mv{manifest['manifest_version']}",
        )

        # 3. --------------------------------------------- launch the engine
        r = c.post("/api/browser/launch", headers=headers)
        assert r.status_code == 200, r.text
        st = r.json()["status"]
        print(
            "     status:",
            {k: st[k] for k in ("running", "engine_active", "kdspy_loaded", "engine_note")},
        )
        assert st["running"] is True
        assert st["engine_active"] == "camoufox", st
        assert st["kdspy_loaded"] is True, st  # add-on active in the engine
        ok("3 camoufox engine launched with the add-on loaded")

        # Durable add-on state must now be 'validated' (post-launch record).
        r = c.get("/api/browser/extensions/kdspy", headers=headers)
        assert r.status_code == 200, r.text
        fx = r.json()["firefox_addon"]
        assert fx["status"] == "validated", fx
        ok("4 add-on durable state validated", f"v{fx.get('version', '')}")

        # 5. ----------------------------------- open the sign-in browser
        r = c.post(
            "/api/browser/interactive/start",
            headers=headers,
            json={"purpose": "kdspy_setup"},
        )
        assert r.status_code == 200, r.text
        sess = r.json()["session"]
        print("     interactive:", sess["purpose"], sess["status"], sess.get("url", ""))
        assert sess["status"] == "open", sess
        ok("5 sign-in browser open (kdspy_setup → kdspy.com)")

        # 6. --------------------------------------------- rendered frame
        r = c.get("/api/browser/interactive/frame", headers=headers)
        assert r.status_code == 200, r.text
        frame = base64.b64decode(r.json()["frame"])
        frame_path = tmp / "kdspy_signin_frame.jpg"
        frame_path.write_bytes(frame)
        assert len(frame) > 5000, f"frame suspiciously small: {len(frame)}"
        ok("6 live frame rendered", f"{len(frame):,} bytes → {frame_path}")

        # 7. ---------------------------- spoofed identity inside the browser
        portal = getattr(c, "portal", None)
        assert portal is not None, "TestClient portal unavailable"
        from app import camoufox_engine as ce

        config = config_mod.get_config()
        fp = ce.load_or_generate_fingerprint(config)

        async def _identity() -> dict:
            mgr = app.state.browser_manager
            ctx = mgr._context
            page = ctx.pages[-1]
            return {
                "ua": await page.evaluate("navigator.userAgent"),
                "platform": await page.evaluate("navigator.platform"),
                "webdriver": await page.evaluate("navigator.webdriver"),
                "url": page.url,
            }

        ident = portal.call(_identity)

        def ua_shape(s: str) -> str:
            return re.sub(r"(rv:|Firefox/)\d+(\.\d+)*", r"\1X", s)

        assert not ident["webdriver"], ident
        assert ident["platform"] == fp.navigator.platform, (
            ident["platform"],
            fp.navigator.platform,
        )
        assert ua_shape(ident["ua"]) == ua_shape(fp.navigator.userAgent), (
            ident["ua"],
            fp.navigator.userAgent,
        )
        ok(
            "7 identity coherent (UA shape + platform + webdriver hidden)",
            ident["ua"][:60],
        )

    print("\nE2E CORE FLOW OK —", len(steps), "steps passed")
    return 0


# -------------------------------------------------------------------- parent
def main() -> int:
    if "--child" in sys.argv:
        return run_e2e()

    # Parent: run the child as the preview server sees it — HOME=/root, i.e.
    # the account WITHOUT the camoufox download in its cache. The engine's
    # cache-resolution fix must make the whole flow work anyway.
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": "/root",
        "LANG": "C.UTF-8",
    }
    print("== E2E under preview-like env (HOME=/root, camoufox cache elsewhere) ==\n")
    proc = subprocess.run(
        [PY, __file__, "--child"],
        capture_output=True,
        text=True,
        timeout=420,
        env=env,
    )
    print(proc.stdout)
    if proc.returncode != 0:
        print("STDERR:", proc.stderr[-1500:])
        return proc.returncode or 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
