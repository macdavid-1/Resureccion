"""Amazon's location + currency must follow the marketplace, not the egress IP.

A Nigerian exit IP makes amazon.com show NGN prices and "Deliver to Nigeria".
The fix uses Amazon's own controls — the ``i18n-prefs`` currency cookie and the
"Deliver to" zip dialog — and the browser is pinned to New York once per launch.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import amazon_location
from app.amazon_location import (
    cookie_domain,
    pin_delivery_location,
    read_location,
)


class _El:
    def __init__(self, page: _Page, selector: str) -> None:
        self.page = page
        self.selector = selector

    async def inner_text(self) -> str:
        return self.page.header

    async def click(self, **_kwargs) -> None:
        self.page.clicked.append(self.selector)

    async def fill(self, value: str, **_kwargs) -> None:
        self.page.typed.append(value)
        self.page.pinned = True
        # Amazon's header reflects the applied zip.
        self.page.header = f"Deliver to New York {value}"


class _Context:
    def __init__(self) -> None:
        self.cookies: list[dict] = []

    async def add_cookies(self, cookies) -> None:
        self.cookies.extend(cookies)


class _Page:
    """A minimal Amazon page shaped by which selectors it answers to."""

    def __init__(
        self,
        url: str,
        *,
        header: str = "Deliver to Nigeria",
        popover: bool = True,
        zip_input: bool = True,
    ) -> None:
        self.url = url
        self.header = header
        self.popover = popover
        self.zip_input = zip_input
        self.has_input = zip_input
        self.pinned = False
        self.typed: list[str] = []
        self.clicked: list[str] = []
        self.visited: list[str] = []
        self.context = _Context()

    async def query_selector(self, selector: str):
        if selector == amazon_location.LOCATION_LINK:
            return _El(self, selector) if self.popover else None
        if selector in amazon_location.ZIP_INPUTS:
            return _El(self, selector) if self.has_input else None
        if selector in amazon_location.ZIP_APPLY:
            return _El(self, selector) if self.has_input else None
        if selector in amazon_location.ZIP_DONE:
            return None
        if selector in amazon_location.HEADER_LOCATION:
            # Amazon's header updates only after the zip is applied.
            return _El(self, selector) if self.header else None
        return None

    async def wait_for_timeout(self, _ms: int) -> None:
        return None

    async def goto(self, url: str, **_kwargs) -> None:
        self.visited.append(url)
        self.url = url
        self.has_input = self.zip_input


# ------------------------------------------------------------------- cookies
def test_the_currency_cookie_is_scoped_to_the_marketplace_domain() -> None:
    assert cookie_domain("https://www.amazon.com/s?k=x") == ".amazon.com"
    assert cookie_domain("https://www.amazon.co.uk/dp/B0") == ".amazon.co.uk"
    assert cookie_domain("") == ""


@pytest.mark.asyncio
async def test_prefs_write_currency_and_language_for_this_marketplace() -> None:
    page = _Page("https://www.amazon.com/s?k=grief")
    assert await amazon_location.apply_market_prefs(
        page, currency="GBP", language="en_GB"
    )
    cookies = {c["name"]: c["value"] for c in page.context.cookies}
    assert cookies == {"i18n-prefs": "GBP", "lc-main": "en_GB"}
    assert {c["domain"] for c in page.context.cookies} == {".amazon.com"}


# -------------------------------------------------------------------- the pin
@pytest.mark.asyncio
async def test_the_zip_dialog_pins_the_delivery_location() -> None:
    page = _Page("https://www.amazon.com/s?k=grief")
    status = await pin_delivery_location(page, zip_code="10001", currency="USD")

    assert status["ok"] is True, status
    assert "10001" in status["location"]
    assert page.typed == ["10001"]
    assert amazon_location.LOCATION_LINK in page.clicked
    assert amazon_location.ZIP_APPLY[0] in page.clicked
    assert not page.visited, "the header popover needs no navigation"


@pytest.mark.asyncio
async def test_an_already_pinned_location_costs_nothing() -> None:
    page = _Page("https://www.amazon.com/", header="Deliver to New York 10001")
    status = await pin_delivery_location(page, zip_code="10001")
    assert status["ok"] is True
    assert status["detail"] == "delivery location already set"
    assert page.typed == [] and page.clicked == []


@pytest.mark.asyncio
async def test_a_missing_popover_falls_back_to_the_address_change_page() -> None:
    page = _Page(
        "https://www.amazon.com/s?k=grief", popover=False, zip_input=False
    )

    async def goto(url, **kwargs):
        page.visited.append(url)
        page.has_input = True
        page.header = "Deliver to New York 10001"

    page.goto = goto  # type: ignore[method-assign]

    status = await pin_delivery_location(page, zip_code="10001")
    assert status["ok"] is True, status
    assert page.visited and "address-change" in page.visited[0]
    assert page.visited[0].startswith("https://www.amazon.com/")


@pytest.mark.asyncio
async def test_an_unconfirmable_pin_is_reported_not_claimed() -> None:
    page = _Page("https://www.amazon.com/s?k=grief", zip_input=False, popover=False)
    status = await pin_delivery_location(page, zip_code="10001")
    assert status["ok"] is False
    assert "not confirmed" in status["detail"]
    assert status["location"] == "Deliver to Nigeria"


@pytest.mark.asyncio
async def test_a_broken_page_never_raises_and_never_navigates() -> None:
    class _Broken:
        url = "https://www.amazon.com/"

        async def query_selector(self, _selector):
            raise RuntimeError("page crashed")

        async def goto(self, *_a, **_k):
            raise AssertionError("a broken page must not be navigated")

    status = await pin_delivery_location(_Broken(), zip_code="10001")
    assert status["ok"] is False
    assert status["detail"]

    stub = SimpleNamespace()
    status = await pin_delivery_location(stub, zip_code="10001")
    assert status["ok"] is False and "DOM" in status["detail"]


@pytest.mark.asyncio
async def test_the_header_reader_prefers_the_delivery_line() -> None:
    page = _Page("https://www.amazon.com/", header="Deliver to New York 10001")
    assert await read_location(page) == "Deliver to New York 10001"


# ------------------------------------------------------------ manager wiring
def _manager(**overrides):
    from app import browser_manager as bm

    mgr = bm.BrowserManager.__new__(bm.BrowserManager)
    mgr.config = SimpleNamespace(
        amazon_location_pin=True, amazon_delivery_zip="10001", **overrides
    )
    mgr._location_pinned = False
    mgr._location_note = None
    mgr._location_text = None
    mgr._touch = lambda: None
    return mgr


@pytest.mark.asyncio
async def test_the_manager_pins_once_per_launch(monkeypatch) -> None:
    mgr = _manager()
    calls: list[dict] = []

    async def fake_pin(page, **kwargs):
        calls.append(kwargs)
        return {"ok": True, "location": "Deliver to New York 10001", "detail": ""}

    monkeypatch.setattr(amazon_location, "pin_delivery_location", fake_pin)
    page = _Page("https://www.amazon.com/s?k=grief")

    await mgr._pin_market_location(page, "https://www.amazon.com/s?k=grief")
    await mgr._pin_market_location(page, "https://www.amazon.com/s?k=again")

    assert len(calls) == 1, "the pin must not cost a page load per navigation"
    assert calls[0]["zip_code"] == "10001"
    assert calls[0]["currency"] == "USD"  # the marketplace's own currency
    assert "New York" in (mgr._location_note or "")
    assert mgr._location_text == "Deliver to New York 10001"


@pytest.mark.asyncio
async def test_a_uk_marketplace_gets_gbp_not_usd(monkeypatch) -> None:
    mgr = _manager()
    seen: dict = {}

    async def fake_prefs(page, **kwargs):
        seen.update(kwargs)
        return True

    monkeypatch.setattr(amazon_location, "apply_market_prefs", fake_prefs)
    page = _Page("https://www.amazon.co.uk/s?k=grief")
    await mgr._prime_market_location(page, "https://www.amazon.co.uk/s?k=grief")

    assert seen["currency"] == "GBP"
    assert seen["language"] == "en_GB"
    assert seen["url"].startswith("https://www.amazon.co.uk")


@pytest.mark.asyncio
async def test_non_amazon_pages_are_left_alone(monkeypatch) -> None:
    mgr = _manager()
    called: list[str] = []

    async def fake_pin(_page, **_kwargs):
        called.append("pin")
        return {"ok": True, "location": "", "detail": ""}

    monkeypatch.setattr(amazon_location, "pin_delivery_location", fake_pin)
    page = _Page("https://example.com/")
    await mgr._pin_market_location(page, "https://example.com/")
    assert called == []
    assert mgr._location_note is None
