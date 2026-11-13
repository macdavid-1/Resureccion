"""Pin Amazon's delivery location + currency to the marketplace being researched.

Amazon decides BOTH the currency and the delivery location from the visitor's
IP. A Nigerian egress therefore gets NGN prices and "Deliver to Nigeria" even
on amazon.com — and every price, royalty assumption and buyer-intent signal in
the report is then quoted in the wrong market.

No new service is involved: Amazon's own header control does this. Currency
lives in the ``i18n-prefs`` cookie, and the delivery zip is set through the
"Deliver to" dialog Amazon ships on every page (``GLUXZipUpdateInput``), which
is then stored in the site's persistent session — and the browser profile is
persistent, so the pin usually survives later runs.

Everything here is best-effort by design: a location that cannot be pinned is
reported (`ok: False` with what was actually seen), never raised. Research must
never fail because a cookie or a popover did not cooperate.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

# Manhattan, New York — the owner's reference market for US research.
DEFAULT_ZIP = "10001"
DEFAULT_CURRENCY = "USD"
DEFAULT_LANGUAGE = "en_US"

# The header "Deliver to …" control and the zip dialog it opens. Ids are stable
# across Amazon locales (button TEXT is not: "Apply" / "Anwenden" / "Aplicar").
LOCATION_LINK = "#nav-global-location-popover-link"
ZIP_INPUTS = (
    "#GLUXZipUpdateInput",
    "#GLUXZipUpdateInput_0",
    "input[name='zipCode']",
    "input[autocomplete='postal-code']",
)
ZIP_APPLY = (
    "#GLUXZipUpdate",
    "input[aria-labelledby='GLUXZipUpdate-announce']",
    "#GLUXSubmit",
    "input[type='submit'][name='glow-zip']",
)
ZIP_DONE = (
    "#GLUXConfirmClose",
    "button[name='glowDoneButton']",
    "#GLUXDoneButton",
    "#GLUXConfirmClose-announce",
)
HEADER_LOCATION = (
    "#glow-ingress-line2",
    "#contextualIngressPtLabel",
    "#nav-global-location-slot",
)

ADDRESS_CHANGE_PATH = "/portal-migration/hz/glow/address-change?actionSource=glow"


def cookie_domain(url: str) -> str:
    """Cookie domain for an Amazon host: ``https://www.amazon.co.uk`` →
    ``.amazon.co.uk`` (a leading ``www.`` would scope the cookie to one host)."""
    host = (urlparse(str(url or "")).hostname or "").lower().strip(".")
    if not host:
        return ""
    host = host.removeprefix("www.")
    return f".{host}"


async def read_location(page: Any) -> str:
    """The header's own "Deliver to …" text ('' when it cannot be read)."""
    for selector in HEADER_LOCATION:
        try:
            el = await page.query_selector(selector)
            if el is None:
                continue
            text = " ".join(((await el.inner_text()) or "").split())
            if text:
                return text
        except Exception:
            continue
    return ""


async def apply_market_prefs(
    page: Any,
    *,
    url: str | None = None,
    currency: str = DEFAULT_CURRENCY,
    language: str = DEFAULT_LANGUAGE,
) -> bool:
    """Set the currency/language cookies for this marketplace's domain.

    ``i18n-prefs`` is the preference Amazon's own currency switcher writes;
    ``lc-main`` pins the language so prices render with the market's separators
    instead of the visitor's IP locale. Returns True when the cookies landed.
    """
    domain = cookie_domain(url or getattr(page, "url", "") or "")
    if not domain:
        return False
    context = getattr(page, "context", None)
    if context is None or not hasattr(context, "add_cookies"):
        return False
    try:
        await context.add_cookies(
            [
                {"name": "i18n-prefs", "value": str(currency), "domain": domain, "path": "/"},
                {"name": "lc-main", "value": str(language), "domain": domain, "path": "/"},
            ]
        )
        return True
    except Exception:
        return False


async def _first(page: Any, selectors: tuple[str, ...]) -> Any:
    for selector in selectors:
        try:
            el = await page.query_selector(selector)
        except Exception:
            el = None
        if el is not None:
            return el
    return None


async def _click(handle: Any) -> bool:
    try:
        await handle.click(timeout=4000)
        return True
    except Exception:
        return False


async def pin_delivery_location(
    page: Any,
    *,
    zip_code: str = DEFAULT_ZIP,
    currency: str = DEFAULT_CURRENCY,
    language: str = DEFAULT_LANGUAGE,
    timeout_ms: int = 6000,
) -> dict[str, Any]:
    """Set Amazon's delivery location to ``zip_code`` on the page's marketplace.

    Returns ``{"ok", "zip", "location", "currency", "detail"}``. ``ok`` is only
    True when the header itself confirms the zip (or already showed it), so a
    silent popover failure is visible instead of assumed.
    """
    zip_code = str(zip_code or "").strip()
    status: dict[str, Any] = {
        "ok": False,
        "zip": zip_code,
        "location": "",
        "currency": str(currency or ""),
        "detail": "",
    }
    if not zip_code:
        status["detail"] = "no delivery zip configured"
        return status
    if not hasattr(page, "query_selector") or not hasattr(page, "goto"):
        # Not a real browser page (a stub in tests, or a closed target): there
        # is nothing to inspect and no reason to spend a navigation on it.
        status["detail"] = "page does not expose a DOM"
        return status

    status["currency_cookie"] = await apply_market_prefs(
        page, currency=currency, language=language
    )

    current = await read_location(page)
    status["location"] = current
    if current and zip_code in current:
        status.update(ok=True, detail="delivery location already set")
        return status

    notes: list[str] = []
    # 1) The header popover ("Deliver to …"): no navigation, works on any page.
    try:
        link = await _first(page, (LOCATION_LINK,))
        if link is not None and await _click(link):
            await page.wait_for_timeout(600)
        zinput = await _first(page, ZIP_INPUTS)
        # 2) The standalone address-change page when the popover did not render
        #    (a bot-check-free page, and the flow Amazon's own header uses).
        if zinput is None:
            base = f"{urlparse(getattr(page, 'url', '') or '').scheme or 'https'}://" \
                   f"{urlparse(getattr(page, 'url', '') or '').hostname or ''}"
            await page.goto(f"{base}{ADDRESS_CHANGE_PATH}", wait_until="domcontentloaded")
            zinput = await _first(page, ZIP_INPUTS)
        if zinput is None:
            notes.append("zip input not found")
        else:
            try:
                await zinput.fill(zip_code, timeout=timeout_ms)
            except Exception as exc:  # pragma: no cover - defensive
                notes.append(f"zip fill failed: {type(exc).__name__}")
            apply = await _first(page, ZIP_APPLY)
            if apply is not None and await _click(apply):
                await page.wait_for_timeout(1200)
                done = await _first(page, ZIP_DONE)
                if done is not None:
                    await _click(done)
                    await page.wait_for_timeout(800)
            else:
                notes.append("apply control not found")
        after = await read_location(page)
        status["location"] = after
        if after and zip_code in after:
            status.update(ok=True, detail="delivery location pinned")
        else:
            status["detail"] = (
                "delivery location not confirmed"
                + (f" ({'; '.join(notes)})" if notes else "")
                + (f"; header shows {after!r}" if after else "; header unreadable")
            )
    except Exception as exc:  # a pin failure must never fail research
        status["detail"] = f"{type(exc).__name__}: {exc}"[:200]
    return status
