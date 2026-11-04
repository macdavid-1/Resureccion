"""LIVE amazon.com check for the shipping-overlay dismissal (opt-in).

Runs against the real Amazon search page and injects the interstitial in
Amazon's own structure, because a fresh profile on a datacenter IP is not
always shown the "ship to" tooltip. Enable with:

    RESURRECCION_LIVE_BROWSER=1 pytest tests/test_overlay_live_amazon.py

Skipped by default: it needs the public network and a launchable browser.
Most valuable as a smoke test before a long research run.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from app.browser_manager import (
    _OVERLAY_GUARD_JS,
    BrowserManager,
    _overlay_present,
    _overlay_script,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("RESURRECCION_LIVE_BROWSER") != "1",
    reason="live browser test: set RESURRECCION_LIVE_BROWSER=1 to run",
)

SEARCH_URL = "https://www.amazon.com/s?k=pesticide+applicator+exam+prep"

# Amazon's real interstitial structure, dropped into the live page over the
# printed result total. Solid magenta so a capture can be checked pixel-wise.
INJECT_JS = """() => {
  const old = document.getElementById('ship-banner');
  if (old) old.remove();
  const slot = document.querySelector('#nav-global-location-slot') || document.body;
  const node = document.createElement('div');
  node.innerHTML = `
    <div class="a-popover a-popover-trigger" id="ship-banner"
         style="position:fixed;top:120px;left:120px;width:470px;height:130px;
                background:rgb(255,0,170);z-index:99999;padding:14px">
      <div class="a-popover-inner">
        We're showing you items that ship to Nigeria. To see items that ship to
        a different country, change your delivery address.
        <span class="a-button a-button-primary a-button-small" id="dismiss-btn">
          <span class="a-button-inner">
            <input class="a-button-input" type="submit" aria-labelledby="dismiss-label">
            <span class="a-button-text" aria-hidden="true" id="dismiss-label">Dismiss</span>
          </span>
        </span>
        <span class="a-button a-button-base" id="change-btn">
          <span class="a-button-inner"><span class="a-button-text">Change Address</span></span>
        </span>
      </div>
    </div>`;
  const banner = node.firstElementChild;
  banner.querySelector('#dismiss-btn').addEventListener('click', () => banner.remove());
  slot.appendChild(banner);
  document.body.dataset.injected = '1';
  return true;
}"""


def test_live_amazon_results_page_is_captured_without_the_shipping_overlay(config) -> None:
    from io import BytesIO

    from PIL import Image

    camoufox = pytest.importorskip("camoufox.async_api")

    async def run() -> str:
        manager = camoufox.AsyncCamoufox(headless=True, geoip=False, humanize=False)
        async with manager as browser:
            page = await browser.new_page()
            await page.set_viewport_size({"width": 1280, "height": 900})
            await page.add_init_script(_overlay_script(_OVERLAY_GUARD_JS))
            await page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(1500)

            body = await page.inner_text("body")
            if "Enter the characters you see below" in body or "api-services-support" in body:
                pytest.skip("Amazon served a bot wall from this host")

            # No false positive on the genuine page.
            assert await _overlay_present(page) is False, (
                "the detector claimed an overlay on a page with none"
            )

            await page.evaluate(INJECT_JS)
            assert await page.evaluate("() => document.body.dataset.injected") == "1", (
                "injection failed"
            )
            # The in-page guard may already have dismissed it — that is the
            # intended behaviour, so only the end state is asserted.

            png = await BrowserManager(config, None, None).screenshot(page)
            assert await _overlay_present(page) is False, "overlay survived the capture"
            assert await page.evaluate(
                "() => { const n = document.getElementById('ship-banner');"
                " return !n || n.getAttribute('data-resur-hidden') === '1'; }"
            ), "the interstitial is still on the page"
            image = Image.open(BytesIO(png)).convert("RGB")
            colors = image.getcolors(maxcolors=image.width * image.height + 1) or []
            assert not any(color == (255, 0, 170) for _c, color in colors), (
                "the captured PNG still shows the interstitial"
            )

        # Report the printed total the research reads, for the log.
        return body

    body = asyncio.run(run())
    assert "results" in body.lower()
