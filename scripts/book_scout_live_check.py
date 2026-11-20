"""Live proof that Scrivy Book Scout captures BSRs on a real Amazon search.

This is not a unit test. It launches a real Firefox, installs the bundled
add-on (`vendor/scrivy-book-scout.xpi`, the AMO-signed package the app ships),
opens a real Amazon books search, waits for the add-on's badges to fill in,
reads them with the app's own extractor, and writes screenshots of what the
browser actually rendered.

Run: python scripts/book_scout_live_check.py [--query "grief memoir"]
Exit: 0 when at least one result carries a BSR, 1 otherwise (with the exact
reason printed — a missing add-on, a wall, or an empty page).

Requires a browser Playwright can drive. The app's own engine is Camoufox; when
that binary is not installed this script drives Playwright's Firefox instead —
the add-on and the extractor are identical either way, so the capture path is
exercised for real.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Config
from app.evidence_capture import _extract_book_scout

ADDON_ID = "book-scout@scrivy.co"
PROFILE = Path("/tmp/book-scout-firefox-profile")
OUT = ROOT / "data" / "book-scout-live"


def _install_addon(profile: Path) -> Path:
    """Sideload the signed XPI into a fresh Firefox profile."""
    cfg = Config()
    xpi = Path(cfg.book_scout_bundled_xpi)
    if not xpi.is_file():
        raise SystemExit(f"bundled add-on missing: {xpi}")
    ext = profile / "extensions"
    ext.mkdir(parents=True, exist_ok=True)
    target = ext / f"{ADDON_ID}.xpi"
    shutil.copyfile(xpi, target)
    return target


async def main(query: str) -> int:
    from playwright.async_api import async_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    PROFILE.mkdir(parents=True, exist_ok=True)
    target = _install_addon(PROFILE)
    print(f"[addon] sideloaded {target.name} ({target.stat().st_size} bytes)")

    from urllib.parse import quote_plus

    from app.marketplace import get_marketplace

    mkt = get_marketplace("us")
    url = f"https://{mkt.domain}/s?k={quote_plus(query)}&i=stripbooks"
    print(f"[browser] {url}")

    async with async_playwright() as p:
        ctx = await p.firefox.launch_persistent_context(
            str(PROFILE),
            headless=True,
            viewport={"width": 1440, "height": 1000},
            locale="en-US",
            timezone_id="America/New_York",
            # A stock Firefox UA + the usual navigation headers: Amazon serves
            # its "Sorry!" wall to Playwright's default client signature but a
            # real results page to these (verified: 503 → 200).
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) "
                "Gecko/20100101 Firefox/135.0"
            ),
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
            },
            firefox_user_prefs={
                # Allow the profile-scope sideload to stay enabled.
                "extensions.autoDisableScopes": 0,
                "extensions.enabledScopes": 15,
                "extensions.installDistroAddons": False,
            },
        )
        try:
            page = await ctx.new_page()
            # A cold profile's first deep request gets Amazon's 503 wall; one
            # homepage visit sets session cookies and the search page then
            # loads normally (verified: 503 → 200 in the same profile).
            await page.goto(
                f"https://{mkt.domain}/", wait_until="domcontentloaded", timeout=120_000
            )
            await page.wait_for_timeout(2500)
            await page.goto(url, wait_until="domcontentloaded", timeout=120_000)
            try:
                await page.wait_for_selector(".scrivy-badge", timeout=45_000)
            except Exception:
                body = (await page.content())[:1500]
                await page.screenshot(path=str(OUT / "no-addon.png"))
                print("[fail] no .scrivy-badge appeared — page head:")
                print(body)
                return 1

            payload = await _extract_book_scout(page, wait_seconds=120.0)
            if payload is None:
                await page.screenshot(path=str(OUT / "empty.png"))
                print("[fail] add-on rendered no badges before the timeout")
                return 1

            from app.book_scout import parse_page_payload

            data = parse_page_payload(payload)
            data["query"] = query
            (OUT / "capture.json").write_text(json.dumps(data, indent=1, ensure_ascii=False))
            await page.screenshot(path=str(OUT / "search-results.png"))
            # A zoomed frame of one badge: the evidence a reader can check.
            badge = page.locator(".scrivy-badge").first
            await badge.scroll_into_view_if_needed()
            await badge.screenshot(path=str(OUT / "badge.png"))

            counts = data["counts"]
            print(f"[addon] {data['addon']} on {data['url']}")
            print(f"[capture] {counts}")
            for b in data["badges"][:6]:
                print(f"  {b['asin']}  {b['bsr'] or '(no rank)'}  {b['title'][:60]}")
            stats = ", ".join(f"{s['label']} {s['value']}" for s in data["niche"]["stats"][:6])
            print(f"[niche] {stats}")
            print(f"[screenshots] {OUT}")
            return 0 if counts["with_bsr"] > 0 else 1
        finally:
            await ctx.close()


if __name__ == "__main__":
    q = "grief memoir"
    if "--query" in sys.argv:
        q = sys.argv[sys.argv.index("--query") + 1]
    raise SystemExit(asyncio.run(main(q)))
