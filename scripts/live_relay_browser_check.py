"""Live verification: Chromium egresses through the relay shim with privacy
flags applied; a real page load flows shim→hub→(fallback)→origin.

Uses an isolated DATA_DIR so the running preview's durable profile is never
touched. Run: .venv/bin/python scripts/live_relay_browser_check.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, ".")


async def main() -> int:
    os.environ["RELAY_ENABLED"] = "true"
    os.environ["RELA"] = ""  # (no-op; explicit for clarity)
    tmp = tempfile.mkdtemp(prefix="relay_check_")
    os.environ["DATA_DIR"] = tmp
    os.environ["BROWSER_HEADLESS"] = "true"

    from app import config as cm

    cm.get_config.cache_clear()
    from app.config import get_config
    from app.db import init_db
    from app.browser_store import BrowserEvidenceStore, ExtensionStore
    from app.browser_manager import BrowserManager
    from app.kdspy import KDSpyManager
    from app.relay import RelayHub

    cfg = get_config()
    cfg.ensure_dirs()
    db = init_db(cfg)
    hub = RelayHub(cfg)
    await hub.start()
    kdspy = KDSpyManager(cfg, ExtensionStore(db))
    mgr = BrowserManager(cfg, kdspy, BrowserEvidenceStore(db), relay=hub)
    st = await mgr.launch()
    print("browser running:", st.running, "| relay shim:", hub.shim.port if hub.shim else None)

    page = await mgr.new_page()
    await page.goto("http://example.com", timeout=60000)
    title = await page.title()
    print("page title via relay shim:", title)
    print(
        "shim stats:",
        {k: hub.stats[k] for k in ("relay_served", "fallback_direct", "blocked_requests", "connections_total")},
    )

    await page.close()
    await mgr.shutdown()
    await hub.stop()
    db.close()
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
