"""Live check: one-tap AMO install + real Camoufox launch with the add-on.

Exercises the full owner flow end to end — fetch_amo_xpi() downloads the
pinned KDSpy XPI from addons.mozilla.org, install_from_amo() validates and
installs it, then BrowserManager launches the camoufox engine with the add-on
loaded and navigates a page.

Run: .venv/bin/python scripts/amo_live_check.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _sandbox_env_shims() -> None:
    """Dev-sandbox only: run as root with the browser owned by another user.

    Same shims as scripts/camoufox_app_smoke.py; in deployment (HF Spaces /
    the Dockerfile) the app runs as the user that fetched Camoufox itself.
    """
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        import glob as _g

        for pat in (
            "/home/*/.cache/camoufox/browsers/*/*/camoufox-bin",
            os.path.expanduser("~/.cache/camoufox/browsers/*/*/camoufox-bin"),
        ):
            hits = _g.glob(pat)
            if hits:
                os.environ.setdefault(
                    "XDG_CACHE_HOME", str(Path(hits[0]).parents[3].parent)
                )
                break
        os.environ["HOME"] = "/root"
        try:
            os.makedirs("/root/.camoufox", exist_ok=True)
        except OSError:
            pass


async def main() -> int:
    _sandbox_env_shims()

    from app.browser_manager import BrowserManager
    from app.browser_store import BrowserEvidenceStore, ExtensionStore
    from app.config import Config
    from app.db import Database
    from app.kdspy import KDSpyManager, fetch_amo_xpi

    cfg = Config()
    tmp = tempfile.mkdtemp(prefix="amo_live_")
    cfg.data_dir = Path(tmp)
    cfg.browser_profiles_dir = Path(tmp) / "browser_profiles"
    cfg.kdspy_firefox_path = Path(tmp) / "extensions" / "kdspy-firefox"
    cfg.browser_headless = True

    print("pinned AMO URL:", cfg.kdspy_amo_url)
    raw = await fetch_amo_xpi(cfg.kdspy_amo_url)
    ok_magic = raw[:4] == b"PK\x03\x04"
    print(f"fetched {len(raw):,} bytes; ZIP magic ok: {ok_magic}")
    if not ok_magic:
        return 1

    db = Database(Path(tmp) / "state.db")
    db.connect()
    kdspy = KDSpyManager(cfg, ExtensionStore(db))
    info = await kdspy.install_from_amo()
    print("installed from AMO:", info.name, "v" + info.version)

    mgr = BrowserManager(cfg, kdspy, BrowserEvidenceStore(db))
    st = await mgr.launch()
    print(f"engine_active={st.engine_active} kdspy_loaded={st.kdspy_loaded}")
    if st.engine_active != "camoufox" or not st.kdspy_loaded:
        print("FAIL: camoufox engine did not launch with the add-on")
        return 1
    state = kdspy.firefox_addon_state()
    print("durable add-on state:", state.status, state.version)

    page = await mgr.new_page()
    await page.goto("https://example.com", timeout=60000)
    ua = await page.evaluate("navigator.userAgent")
    print("page navigated; spoofed UA:", ua[:60])

    await mgr.shutdown()
    db.close()
    print("AMO ONE-TAP + CAMOUFOX LIVE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
