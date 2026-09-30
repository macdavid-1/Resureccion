"""Live E2E: reproduce the NS_ERROR_PROXY_CONNECTION_REFUSED incident.

Sets BROWSER_PROXY to a dead local port, then drives the REAL app path:
Config → BrowserManager.launch() → open_marketplace(). Before the fix this
died on the first page.goto; now the launch-time gate must demote egress to
direct and the navigation must succeed.

Run: HOME=/root .venv/bin/python scripts/verify_proxy_fallback_e2e.py
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _dead_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def main() -> int:
    dead = _dead_port()
    os.environ["BROWSER_PROXY"] = f"127.0.0.1:{dead}"

    from app.config import get_config
    from app.db import init_db
    from app.browser_manager import BrowserManager, PLAYWRIGHT_AVAILABLE
    from app.browser_store import BrowserEvidenceStore, ExtensionStore
    from app.kdspy import KDSpyManager
    from app.marketplace import get_marketplace

    if not PLAYWRIGHT_AVAILABLE:
        print("playwright not installed — cannot run live E2E")
        return 2

    config = get_config()
    config.ensure_dirs()
    assert config.browser_proxy == f"127.0.0.1:{dead}", "env var not picked up"
    db = init_db(config)
    kdspy = KDSpyManager(config, ExtensionStore(db))
    evidence = BrowserEvidenceStore(db)
    mgr = BrowserManager(config, kdspy, evidence)

    print(f"[1] BROWSER_PROXY = {config.browser_proxy} (dead port)")
    st = await mgr.launch()
    print(f"[2] launch: engine={st.engine_active} egress={st.egress_mode}")
    print(f"    note: {st.egress_note}")
    if st.egress_mode != "direct":
        print("FAIL: egress should be demoted to direct")
        return 1
    if "unreachable" not in (st.egress_note or ""):
        print("FAIL: egress note missing")
        return 1

    mkt = get_marketplace("us")
    page = await mgr.open_marketplace(mkt)
    title = await page.title()
    print(f"[3] open_marketplace OK: {title[:60]!r}")

    await mgr.navigate(page, "https://example.com/")
    print("[4] navigate OK")

    await mgr.shutdown()
    db.close()
    print("PASS: dead proxy demoted to direct; navigation works end to end")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
