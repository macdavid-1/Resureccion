"""The research model's own Playwright verbs — at human pace.

The model is not handed a mock or a shortcut: it drives the SAME persistent
Camoufox browser the rest of the app uses, through a small vocabulary it picks
itself (navigate, click, type, press, scroll). When it sees the shipping-country
notice in a screenshot it can therefore click "Dismiss" the way a person would,
and when a page needs a mouse it never had before, it has one.

Two rules keep that surface useful instead of destructive:

- **Human cadence.** The pointer travels to the element in jittered steps,
  presses, and releases with a pause; typing is per-character; scrolling is
  stepped; navigation settles. Amazon fingerprints input cadence as much as it
  fingerprints the UA, so machine-gun actions would defeat the engine choice.
  ``BROWSER_ACTION_PACE=off`` makes every pause zero — for tests and for an
  owner who explicitly wants the old, fast behaviour.
- **Network scope.** Clicking is unrestricted (the DOM is the model's to
  drive), but navigation is deny-by-default: Amazon marketplaces plus the
  external research allow-list. "Full Playwright access" is access to the page,
  never to arbitrary hosts.

Pure helpers (``looks_like_selector``, ``host_allowed``) are testable without a
browser; the async verbs run against the real engine in tests.
"""
from __future__ import annotations

import asyncio
import os
import random
from collections.abc import Iterable
from typing import Any

# Marks a page the model is driving, so the manager's overlay hygiene knows a
# refusal click may already have been the model's own.
PACE_ENV = "BROWSER_ACTION_PACE"

_SELECTOR_PREFIXES = ("css=", "xpath=", "//", "#", ".", "[", "*", "text=", "role=")


class BrowserActionError(Exception):
    """The model's requested browser action could not be performed."""


# ---------------------------------------------------------------- pacing
def natural_pace() -> bool:
    """True unless the owner turned pacing off (tests set ``off``)."""
    raw = os.environ.get(PACE_ENV, "natural").strip().lower()
    return raw not in ("off", "fast", "0", "false", "no")


async def _pause(lo_ms: float, hi_ms: float) -> None:
    """A human-ish pause between two inputs (no-op when pacing is off)."""
    if not natural_pace():
        return
    await asyncio.sleep(random.uniform(lo_ms, hi_ms) / 1000.0)


# ------------------------------------------------------- target resolution
def looks_like_selector(target: str) -> bool:
    """Is this a CSS/Playwright selector rather than a visible label?

    The model may say ``#dismiss-btn`` or ``a.a-button``, or it may say
    ``Dismiss`` / ``Change Address``. The distinction has to be generous on both
    sides: labels contain spaces, selectors do not (except combinators), and a
    bare word with no CSS punctuation is always a label.
    """
    t = (target or "").strip()
    if not t:
        return False
    if t.startswith(_SELECTOR_PREFIXES):
        return True
    if any(ch.isspace() for ch in t):
        return False
    return any(mark in t for mark in (".", "#", "[", "]", ">", ":", "*", "="))


async def resolve(page: Any, target: str) -> Any:
    """The first element matching a selector or a visible label.

    Order: selector (when it looks like one), then exact text, then partial
    text, then a button/control role by name. The first that matches wins, so a
    label is never misread as a selector and vice versa.
    """
    t = (target or "").strip()
    if not t:
        raise BrowserActionError("a target is required (selector or visible text)")

    candidates: list[Any] = []
    if looks_like_selector(t):
        candidates.append(lambda: page.locator(t))
    candidates.append(lambda: page.get_by_text(t, exact=True))
    candidates.append(lambda: page.get_by_text(t, exact=False))
    candidates.append(lambda: page.get_by_role("button", name=t))

    for build in candidates:
        try:
            locator = build()
            count = await locator.count()
        except Exception:
            continue
        if count:
            return locator.first
    raise BrowserActionError(f"nothing on the page matches {t!r}")


# ------------------------------------------------------------------ verbs
async def _move_pointer(page: Any, box: dict[str, float]) -> None:
    """Travel to a point inside the box the way a hand would."""
    try:
        x = box["x"] + box["width"] * random.uniform(0.35, 0.65)
        y = box["y"] + box["height"] * random.uniform(0.35, 0.65)
        await page.mouse.move(
            x - random.uniform(30, 90), y - random.uniform(20, 60),
            steps=random.randint(3, 6),
        )
        await _pause(40, 120)
        await page.mouse.move(x, y, steps=random.randint(4, 9))
    except Exception:
        pass  # a missing mouse is not a failed click — the locator still has one


async def click(page: Any, target: str) -> str:
    """Click one element (selector or label) with a real pointer path.

    Returns the target text that was clicked, so the run's trace can say
    "clicked Dismiss" instead of "an action ran".
    """
    locator = await resolve(page, target)
    await _pause(120, 320)
    try:
        await locator.scroll_into_view_if_needed(timeout=4000)
    except Exception:
        pass
    await _pause(140, 380)
    box: dict[str, float] | None = None
    try:
        box = await locator.bounding_box()
    except Exception:
        box = None
    if box and natural_pace():
        await _move_pointer(page, box)
        await page.mouse.down()
        await _pause(45, 110)
        await page.mouse.up()
    else:
        await locator.click(timeout=6000)
    await _pause(220, 620)
    return str(target).strip()


async def type_text(page: Any, target: str, text: str) -> str:
    """Focus a field (by selector or label) and type into it character by character."""
    await click(page, target)
    locator = await resolve(page, target)
    await _pause(120, 300)
    try:
        await locator.fill("")  # replacing a value starts by clearing it
    except Exception:
        pass
    delay = random.randint(45, 120) if natural_pace() else 0
    await page.keyboard.type(str(text), delay=delay)
    await _pause(160, 420)
    return str(target).strip()


async def press(page: Any, key: str) -> str:
    """Press one key (Enter, Escape, Tab, Control+A …) — e.g. submitting a search."""
    key = (key or "").strip()
    if not key:
        raise BrowserActionError("press requires a key (e.g. 'Enter')")
    await _pause(90, 240)
    await page.keyboard.press(key)
    await _pause(180, 420)
    return key


async def scroll(page: Any, direction: str = "down", amount: int = 1) -> str:
    """Scroll the viewport in small, human-sized steps."""
    try:
        amount = max(1, int(amount))
    except (TypeError, ValueError):
        amount = 1
    down = str(direction or "down").strip().lower() not in ("up", "-", "back")
    sign = 1 if down else -1
    remaining = 380 * amount
    while remaining > 0:
        step = min(remaining, random.randint(170, 320))
        try:
            await page.mouse.wheel(0, sign * step)
        except Exception as exc:  # no wheel support is a real failure
            raise BrowserActionError(f"the page cannot be scrolled: {exc}") from exc
        remaining -= step
        await _pause(70, 180)
    return "down" if down else "up"


async def navigate(page: Any, url: str, *, allowed_hosts: Iterable[str]) -> str:
    """Go to a URL, then let the page settle before the model looks at it."""
    url = (url or "").strip()
    if not url:
        raise BrowserActionError("navigate requires a url")
    if not host_allowed(url, allowed_hosts):
        raise BrowserActionError(f"navigation host not allow-listed: {url[:120]}")
    await _pause(300, 800)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception as exc:
        raise BrowserActionError(f"navigation failed: {exc}") from exc
    try:
        await page.wait_for_load_state("load", timeout=8000)
    except Exception:
        pass  # a slow third-party asset is not a failed navigation
    await _pause(900, 1800)
    return str(getattr(page, "url", "") or url)


# ------------------------------------------------------------------ hosts
def host_allowed(url: str, allowed_hosts: Iterable[str]) -> bool:
    """Deny-by-default host gate for navigation (subdomains included)."""
    from urllib.parse import urlparse

    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    for allowed in allowed_hosts:
        a = str(allowed or "").strip().lower().lstrip(".")
        if not a:
            continue
        if host == a or host.endswith("." + a):
            return True
    return False
