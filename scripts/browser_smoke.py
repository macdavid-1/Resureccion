"""Live smoke test for the browser infrastructure.

Launches the real persistent Chromium through BrowserManager, opens a
marketplace page, captures a page_state evidence record with screenshot,
then shuts down. Run: python scripts/browser_smoke.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main() -> int:
    from app import config as config_mod

    config_mod.get_config.cache_clear()
    from app.config import get_config
    from app.db import init_db
    from app.browser_manager import BrowserManager, PLAYWRIGHT_AVAILABLE
    from app.browser_store import BrowserEvidenceStore, ExtensionStore
    from app.kdspy import KDSpyManager
    from app.marketplace import get_marketplace

    if not PLAYWRIGHT_AVAILABLE:
        print("playwright not installed — skip live smoke")
        return 0

    config = get_config()
    config.ensure_dirs()
    db = init_db(config)
    kdspy = KDSpyManager(config, ExtensionStore(db))
    evidence = BrowserEvidenceStore(db)
    mgr = BrowserManager(config, kdspy, evidence)

    print("launching persistent Chromium…")
    st = await mgr.launch()
    print(f"running={st.running} headless={st.headless} kdspy={st.kdspy_loaded}")
    ext = kdspy.state()
    print(f"kdspy extension state: {ext.status} (version={ext.version!r})")

    page = await mgr.open_marketplace(get_marketplace("us"), "/")
    await page.wait_for_load_state("domcontentloaded")
    title = await page.title()
    url = page.url
    print(f"navigated: {url!r} title={title!r}")
    png = await mgr.screenshot(page)
    (Path(config.data_dir) / "smoke_screenshot.png").write_bytes(png)
    print(f"screenshot bytes: {len(png)}")

    await page.close()
    await mgr.shutdown()
    db.close()
    print("smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
