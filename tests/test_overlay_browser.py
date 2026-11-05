"""REAL browser tests: Amazon's shipping interstitial never reaches a capture.

These launch the actual Camoufox engine and assert on real PNG pixels, because
the failure being fixed is visual: the "We're showing you items that ship to…"
tooltip sat on top of the printed result total, so a page that said
"1-16 of over 958 results" could be read by the model as a few dozen.

The fixture page reproduces Amazon's real button markup, which is what made the
old detector miss it:

    <span class="a-button"><span class="a-button-inner">
      <input class="a-button-input" type="submit">      ← no text at all
      <span class="a-button-text">Dismiss</span>        ← the label lives here
    </span></span>

Skipped when no browser can be launched on this host (tests never require one).
"""
from __future__ import annotations

import asyncio
import pathlib

import pytest

from app.browser_manager import (
    _OVERLAY_GUARD_JS,
    BrowserManager,
    _overlay_present,
    _overlay_script,
    dismiss_amazon_overlays,
)

# A solid, unmistakable colour for the overlay: any such pixel in a capture
# means the tooltip was on screen when the model would have looked.
BANNER_RGB = (255, 0, 170)

_BASE_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<style>
  body {{ font: 16px sans-serif; margin: 0; background: #ffffff; }}
  #topline {{ margin: 20px; font-size: 18px; }}
  .line {{ height: 26px; width: 620px; background: #eeeeee; margin: 8px 20px; }}
  #ship-banner {{ position: fixed; top: 40px; left: 40px; width: 460px;
                  height: 130px; background: rgb(255, 0, 170); z-index: 99999;
                  padding: 12px; color: #111; }}
</style></head>
<body>
  <div id="topline">1-16 of over 958 results for "pesticide applicator"</div>
  <div class="line"></div><div class="line"></div><div class="line"></div>
  <script>
    // State is recorded on the DOM, not on `window`: Camoufox evaluates page
    // JS in an isolated world that shares the DOM but not window globals.
    document.body.dataset.accepted = '0';   // must stay 0: never accept
    document.body.dataset.shown = '0';
    const BANNER_HTML = `
      <div class="a-popover" id="ship-banner">
        <div class="a-popover-inner">
          We're showing you items that ship to Nigeria. To see items that ship
          to a different country, change your delivery address.
          <span class="a-button a-button-primary a-button-small" id="dismiss-btn">
            <span class="a-button-inner">
              <input class="a-button-input" type="submit" aria-labelledby="dismiss-label">
              <span class="a-button-text" aria-hidden="true" id="dismiss-label">Dismiss</span>
            </span>
          </span>
          <span class="a-button a-button-base" id="change-btn">
            <span class="a-button-inner">
              <span class="a-button-text" aria-hidden="true">Change Address</span>
            </span>
          </span>
        </div>
      </div>`;
    window.showBanner = function () {
      document.querySelectorAll('#ship-banner').forEach((n) => n.remove());
      const holder = document.createElement('div');
      holder.innerHTML = BANNER_HTML;
      const node = holder.firstElementChild;
      if (%(dismissible)s) {
        node.querySelector('#dismiss-btn').addEventListener('click', () => node.remove());
      }
      node.querySelector('#change-btn').addEventListener('click', () => {
        document.body.dataset.accepted = '1';
      });
      document.body.appendChild(node);
      document.body.dataset.shown = String(Number(document.body.dataset.shown || 0) + 1);
      return true;
    };
    // The harness cannot call page functions directly (Camoufox evaluates in
    // an isolated world), so appearances are requested through a DOM event.
    document.addEventListener('resur-show', () => window.showBanner());
    %(auto)s
  </script>
</body></html>
"""


# The production regression, reproduced: the notice is MOUNTED IN A CONTAINER
# THE DETECTOR NEVER LISTED. Amazon renders it as a generated "glow toaster"
# (`#nav-tip-…`, `.nav-tip`), so matching known containers found nothing at
# all and the tooltip stayed on screen; only the notice's own words are stable.
_TOASTER_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<style>
  body { font: 16px sans-serif; margin: 0; background: #ffffff; }
  #topline { margin: 20px; font-size: 18px; }
  #nav-tip-7c1 { position: fixed; top: 40px; left: 40px; width: 460px;
                 height: 130px; background: rgb(255, 0, 170); z-index: 99999;
                 padding: 12px; color: #111; }
</style></head>
<body>
  <div id="topline">1-16 of over 958 results for "pesticide applicator"</div>
  <script>
    document.body.dataset.accepted = '0';
    document.body.dataset.shown = '0';
    const TEMPLATE = document.createElement('div');
    TEMPLATE.innerHTML = `
      <div id="nav-tip-7c1" class="nav-tip">
        <span>
          We're showing you items that ship to Nigeria. To see items that ship
          to a different country, change your delivery address.
        </span>
        <span class="a-button a-button-primary a-button-small" id="dismiss-btn">
          <span class="a-button-inner">
            <input class="a-button-input" type="submit" aria-labelledby="dismiss-label">
            <span class="a-button-text" aria-hidden="true" id="dismiss-label">Dismiss</span>
          </span>
        </span>
        <span class="a-button a-button-base" id="change-btn">
          <span class="a-button-inner">
            <span class="a-button-text" aria-hidden="true">Change Address</span>
          </span>
        </span>
      </div>`;
    document.addEventListener('resur-show', () => {
      document.querySelectorAll('#nav-tip-7c1').forEach((n) => n.remove());
      const node = TEMPLATE.firstElementChild.cloneNode(true);
      node.querySelector('#dismiss-btn').addEventListener('click', () => node.remove());
      node.querySelector('#change-btn').addEventListener('click', () => {
        document.body.dataset.accepted = '1';
      });
      document.body.appendChild(node);
      document.body.dataset.shown = String(Number(document.body.dataset.shown || 0) + 1);
    });
    setTimeout(() => document.dispatchEvent(new Event('resur-show')), 700);
  </script>
</body></html>
"""


def _write_page(tmp: pathlib.Path, *, dismissible: bool = True, auto: bool = True) -> str:
    html = _BASE_HTML % {
        "dismissible": "true" if dismissible else "false",
        "auto": "setTimeout(() => window.showBanner(), 700);" if auto else "",
    }
    path = tmp / f"page-{int(dismissible)}-{int(auto)}.html"
    path.write_text(html, encoding="utf-8")
    return path.as_uri()


@pytest.fixture(scope="module")
def loop():
    lp = asyncio.new_event_loop()
    try:
        yield lp
    finally:
        lp.close()


@pytest.fixture(scope="module")
def browser(loop):
    """One real Camoufox browser for the whole module (skipped if unavailable)."""
    camoufox = pytest.importorskip("camoufox.async_api")
    manager = camoufox.AsyncCamoufox(headless=True, geoip=False, humanize=False)

    async def _launch():
        return await manager.__aenter__()

    async def _close():
        await manager.__aexit__(None, None, None)

    try:
        instance = loop.run_until_complete(_launch())
    except Exception as exc:  # pragma: no cover - host dependent
        pytest.skip(f"camoufox cannot launch on this host: {str(exc)[:200]}")
    try:
        yield instance
    finally:
        try:
            loop.run_until_complete(_close())
        except Exception:
            pass


def _has_banner_pixels(png: bytes) -> bool:
    from io import BytesIO

    from PIL import Image

    image = Image.open(BytesIO(png)).convert("RGB")
    colors = image.getcolors(maxcolors=image.width * image.height + 1) or []
    return any(color == BANNER_RGB for _count, color in colors)


def _manager(config) -> BrowserManager:
    return BrowserManager(config, None, None)  # only screenshot() is exercised


def test_shipping_overlay_is_gone_before_any_screenshot(config, browser, loop, tmp_path) -> None:
    """The screenshot chokepoint must never capture the tooltip."""

    async def run() -> None:
        page = await browser.new_page()
        await page.set_viewport_size({"width": 1280, "height": 800})
        await page.add_init_script(_overlay_script(_OVERLAY_GUARD_JS))
        await page.goto(_write_page(tmp_path, dismissible=True, auto=True))
        await page.wait_for_timeout(1100)  # banner auto-shows at 700 ms
        # It really did render (the fixture counts appearances); the guard may
        # already have removed it, which is the point.
        assert await page.evaluate("() => document.body.dataset.shown") == "1"

        data = await _manager(config).screenshot(page)

        assert not _has_banner_pixels(data), "the captured PNG still shows the tooltip"
        assert await _overlay_present(page) is False
        assert await page.evaluate("() => document.body.dataset.accepted") == "0"

    loop.run_until_complete(run())


def test_guard_fires_for_every_appearance_without_the_agent(browser, loop, tmp_path) -> None:
    """Six appearances, six dismissals — no model action, no explicit call."""

    async def run() -> None:
        page = await browser.new_page()
        await page.set_viewport_size({"width": 1280, "height": 800})
        await page.add_init_script(_overlay_script(_OVERLAY_GUARD_JS))
        await page.goto(_write_page(tmp_path, dismissible=True, auto=False))
        await page.wait_for_timeout(200)

        for i in range(6):
            # A fresh appearance, exactly like a new results page. Nothing on
            # the Python side is called: the in-page guard must do the work.
            await page.evaluate("() => document.dispatchEvent(new Event('resur-show'))")
            assert await _overlay_present(page) is True, f"appearance {i + 1} did not render"
            for _ in range(30):  # up to ~1.5 s, the in-page guard alone must clear it
                await page.wait_for_timeout(50)
                if not await _overlay_present(page):
                    break
            assert await _overlay_present(page) is False, (
                f"appearance {i + 1} was never dismissed"
            )
        assert await page.evaluate("() => document.body.dataset.accepted") == "0"

    loop.run_until_complete(run())


def test_dismiss_clicks_the_refusal_on_amazons_real_button_markup(browser, loop, tmp_path) -> None:
    """The label lives in a sibling span, not in the input — the click follows it."""

    async def run() -> None:
        page = await browser.new_page()
        await page.set_viewport_size({"width": 1280, "height": 800})
        await page.goto(_write_page(tmp_path, dismissible=True, auto=True))
        await page.wait_for_timeout(900)

        labels = await dismiss_amazon_overlays(page)

        assert "dismiss" in labels, labels
        assert await _overlay_present(page) is False
        assert await page.evaluate("() => document.body.dataset.accepted") == "0"

    loop.run_until_complete(run())


def test_an_unclosable_overlay_is_hidden_so_the_capture_stays_clear(config, browser, loop, tmp_path) -> None:
    """If Amazon's own handler ignores the click, the capture is still clean."""

    async def run() -> None:
        page = await browser.new_page()
        await page.set_viewport_size({"width": 1280, "height": 800})
        await page.goto(_write_page(tmp_path, dismissible=False, auto=True))
        await page.wait_for_timeout(900)

        labels = await dismiss_amazon_overlays(page)
        data = await _manager(config).screenshot(page)

        assert "hidden" in labels, labels
        assert not _has_banner_pixels(data), "a stuck tooltip reached the capture"
        assert await page.evaluate("() => document.body.dataset.accepted") == "0"

    loop.run_until_complete(run())


def test_a_clean_results_page_is_left_untouched(browser, loop, tmp_path) -> None:
    """No interstitial, no work: no false positives, no hidden page content."""

    async def run() -> None:
        page = await browser.new_page()
        await page.set_viewport_size({"width": 1280, "height": 800})
        await page.goto(_write_page(tmp_path, dismissible=True, auto=False))
        await page.wait_for_timeout(200)

        assert await dismiss_amazon_overlays(page) == []
        assert await _overlay_present(page) is False
        assert "958" in await page.inner_text("#topline")
        assert await page.is_visible("#topline")

    loop.run_until_complete(run())


def test_the_notice_is_found_in_a_container_the_detector_never_listed(
    config, browser, loop, tmp_path
) -> None:
    """The production regression: the prompt lives in a generated container.

    Its id and class are random per render, so only matching the notice's
    WORDS can find it. This is the case that shipped broken: the tooltip sat
    over "1-16 of over 958 results" and the count was read as 4.
    """

    async def run() -> None:
        path = tmp_path / "toaster.html"
        path.write_text(_TOASTER_HTML, encoding="utf-8")
        page = await browser.new_page()
        await page.set_viewport_size({"width": 1280, "height": 800})
        await page.add_init_script(_overlay_script(_OVERLAY_GUARD_JS))
        await page.goto(path.as_uri())
        await page.wait_for_timeout(1000)  # it auto-shows at 700 ms
        assert await page.evaluate("() => document.body.dataset.shown") == "1"

        # The in-page guard alone must clear it — no model, no Python call.
        for _ in range(40):
            await page.wait_for_timeout(50)
            if not await _overlay_present(page):
                break
        assert await _overlay_present(page) is False, "the tooltip was never dismissed"

        data = await _manager(config).screenshot(page)
        assert not _has_banner_pixels(data), "the captured PNG still shows the tooltip"
        assert await page.evaluate("() => document.body.dataset.accepted") == "0"

    loop.run_until_complete(run())


def test_page_text_that_merely_mentions_delivery_is_never_hidden(browser, loop, tmp_path) -> None:
    """A help paragraph is not the interstitial: nothing may be touched.

    The words alone are not enough — without a refusal control the whole
    notice has to be present before anything is hidden.
    """

    async def run() -> None:
        html = (
            "<html><body>"
            "<p id=help>To send a gift, change your delivery address in Your Account.</p>"
            "<div id=keep>1-16 of over 958 results</div>"
            "</body></html>"
        )
        path = tmp_path / "help.html"
        path.write_text(html, encoding="utf-8")
        page = await browser.new_page()
        await page.add_init_script(_overlay_script(_OVERLAY_GUARD_JS))
        await page.goto(path.as_uri())
        await page.wait_for_timeout(300)

        assert await dismiss_amazon_overlays(page) == []
        assert await page.is_visible("#help")
        assert await page.is_visible("#keep")

    loop.run_until_complete(run())


def test_the_guard_never_touches_a_page_without_the_notice(browser, loop, tmp_path) -> None:
    """A page that merely has a "Dismiss" button must never be clicked."""

    async def run() -> None:
        html = "<html><body><button>Dismiss</button><div id=keep>content</div></body></html>"
        path = tmp_path / "plain.html"
        path.write_text(html, encoding="utf-8")
        page = await browser.new_page()
        await page.add_init_script(_overlay_script(_OVERLAY_GUARD_JS))
        await page.goto(path.as_uri())
        await page.wait_for_timeout(300)

        assert await dismiss_amazon_overlays(page) == []
        assert await page.is_visible("#keep")
        assert await page.is_visible("button")

    loop.run_until_complete(run())
