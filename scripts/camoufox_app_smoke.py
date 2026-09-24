"""Live check: Resurrección's real BrowserManager launches the Camoufox engine.

Exercises the exact production path — app.config → BrowserManager.launch() →
camoufox persistent context (stable cached fingerprint) → page navigation →
spoofed identity — then verifies the profile/fingerprint cache and shuts down.

Run: .venv/bin/python scripts/camoufox_app_smoke.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _sandbox_env_shims() -> None:
    """Dev-sandbox only: run as root with the browser owned by another user.

    In deployment (HF Spaces / the Dockerfile) the app runs as a non-root
    user that fetched Camoufox itself — no shims needed. Here we point the
    camoufox package manager at the existing download (XDG_CACHE_HOME) and
    give the root user a root-owned HOME so Firefox's root check passes.
    """
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        for pat in (
            "/home/*/.cache/camoufox/browsers/*/*/camoufox-bin",
            os.path.expanduser("~/.cache/camoufox/browsers/*/*/camoufox-bin"),
        ):
            import glob as _g

            hits = _g.glob(pat)
            if hits:
                # <XDG_CACHE_HOME>/camoufox/browsers/<repo>/<ver>/camoufox-bin
                # → XDG_CACHE_HOME is 4 levels above the binary's dir.
                os.environ.setdefault("XDG_CACHE_HOME", str(Path(hits[0]).parents[3].parent))
                break
        # Force (not setdefault): HOME already exists in the sandbox env and
        # Firefox refuses to run as root under a HOME owned by another user.
        os.environ["HOME"] = "/root"
        try:
            os.makedirs("/root/.camoufox", exist_ok=True)
        except OSError:
            pass


async def main() -> int:
    _sandbox_env_shims()

    from app import camoufox_engine as ce
    from app import config as config_mod

    config_mod.get_config.cache_clear()
    from app.config import get_config
    from app.db import init_db
    from app.browser_manager import BrowserManager
    from app.browser_store import BrowserEvidenceStore, ExtensionStore
    from app.kdspy import KDSpyManager

    if not ce.CAMOUFOX_AVAILABLE:
        print("camoufox not installed — cannot run live check")
        return 1

    config = get_config()
    config.ensure_dirs()
    db = init_db(config)
    kdspy = KDSpyManager(config, ExtensionStore(db))
    mgr = BrowserManager(config, kdspy, BrowserEvidenceStore(db))

    print(f"engine requested: {config.browser_engine}")
    st = await mgr.launch()
    print(f"running={st.running} engine_active={st.engine_active} headless={st.headless}")
    print(f"engine_note={st.engine_note!r}")
    assert st.engine_active == "camoufox", f"expected camoufox active, got {st.engine_active}"

    ctx = mgr._context
    page = await ctx.new_page()
    await page.goto("https://example.com", timeout=60000)
    print("navigated:", page.url)
    ua = await page.evaluate("navigator.userAgent")
    wd = await page.evaluate("navigator.webdriver")
    platform = await page.evaluate("navigator.platform")
    tz = await page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone")
    print(f"UA: {ua[:70]}")
    print(f"webdriver={wd} platform={platform} tz={tz}")

    fp = ce.load_or_generate_fingerprint(config)
    print(f"cached fingerprint UA: {fp.navigator.userAgent[:70]}")

    # Camoufox deliberately rewrites the UA's Firefox version segment to the
    # REAL engine build (version coherence: JS feature probes must match), so
    # compare the identity-bearing parts: platform token + UA family/OS shape.
    import re as _re

    def _ua_shape(s: str) -> str:
        return _re.sub(r"(rv:|Firefox/)\d+(\.\d+)*", r"\1X", s)

    if _ua_shape(ua) != _ua_shape(fp.navigator.userAgent):
        print(f"MISMATCH: page UA {ua!r} vs fingerprint {fp.navigator.userAgent!r}")
        return 1
    if platform != fp.navigator.platform:
        print(f"MISMATCH: platform {platform!r} != fingerprint {fp.navigator.platform!r}")
        return 1
    if wd:
        print("MISMATCH: navigator.webdriver leaked true")
        return 1
    print("identity coherent: UA shape + platform + webdriver all match")

    # Page lifecycle through the manager's own API.
    p2 = await mgr.new_page()
    await p2.goto("https://example.com", timeout=60000)
    print("new_page path OK:", p2.url)
    await mgr.close_page(p2)

    await page.close()
    await mgr.shutdown()
    db.close()
    print("CAMOUFOX APP SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
