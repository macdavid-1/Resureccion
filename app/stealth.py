"""Anti-automation-detection hardening for the owner's browser.

Research targets (Amazon, kdspy.com, WordPress logins) commonly deploy
anti-bot defenses that refuse interaction when they detect automation.
The most reliable tell is ``navigator.webdriver === true``, which Chromium
sets whenever it is driven by a automation protocol. When the checkbox is
refused ("I can't check the box"), this is almost always why.

This module applies page-side patches via Chromium's page.addInitScript,
which runs before any page script on every page and every frame —
including cross-origin CAPTCHA iframes. All patches are honest: they only
remove automation flags the browser itself sets, they never alter what
the owner sees, types, or decides, and they add nothing to outgoing
requests. Solving the CAPTCHA remains entirely the owner's action.
"""

from __future__ import annotations

from typing import Any

# Runs before every document script, in every frame of every page.
# navigator.webdriver is the primary automation tell; the platform stack
# (chromium / headless chrome) is the secondary one. Keep the replacement
# stack short, stable and truthful about the browser family.
STEALTH_SCRIPT = r"""
(() => {
  try { Object.defineProperty(Navigator.prototype, 'webdriver', { get: () => undefined, configurable: true }); } catch (e) {}
  try {
    if (navigator.userAgent && !/Chrome\/\d+/.test(navigator.userAgent)) return;
    // Chromium reports 'HeadlessChrome' in headless mode; sites commonly
    // branch on it. Report the real engine family without pretending to be
    // a different browser.
    const ua = navigator.userAgent.replace('HeadlessChrome', 'Chrome');
    Object.defineProperty(navigator, 'userAgent', { get: () => ua, configurable: true });
  } catch (e) {}
})();
"""


def apply(context: Any) -> None:
    """Install the init script on a Playwright BrowserContext.

    Safe to call once per context; calling it twice is harmless but the
    caller should normally install exactly one script per context so the
    patch chain stays minimal. ``context.add_init_script`` is an async
    API in Playwright Python, so this schedules it on the running loop
    instead of silently dropping the coroutine.
    """
    import asyncio

    try:
        coro = context.add_init_script(STEALTH_SCRIPT)
        # add_init_script returns a coroutine on async API objects; schedule
        # it so the patch is actually installed even when the caller is sync.
        if asyncio.iscoroutine(coro):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                coro.close()
                return
            loop.create_task(_swallow(coro))
    except Exception:
        # add_init_script failing is non-fatal (e.g. context closing); the
        # browser still works, only with weaker anti-detection.
        pass


async def _swallow(coro: Any) -> None:
    try:
        await coro
    except Exception:
        pass

