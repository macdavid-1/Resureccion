"""Amazon auth DOM classifier — regression tests.

Grounded in a live probe of amazon.com inside the interactive (mobile-UA)
page: the signed-out desktop nav renders `#nav-link-accountList-nav-line-1`
as bare "Hello" (no "sign in" verb anywhere in the label), which the previous
classifier mis-confirmed as signed-in. These tests pin the corrected
behavior: only a NAMED hello ("Hello, Dana") confirms; "Hello" and
"Hello, sign in" do not. The bot-wall interstitial classifies as captcha.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.amazon_auth import AmazonAuthManager, AuthCheck
from app.browser_store import (
    AUTH_CAPTCHA,
    AUTH_CONFIRMED,
    AUTH_LOGIN_REQUIRED,
    AUTH_UNKNOWN,
)
from app.marketplace import AMAZON_MARKETPLACES

US = AMAZON_MARKETPLACES["us"]


class FakeEl:
    def __init__(self, text: str) -> None:
        self._text = text

    async def inner_text(self) -> str:
        return self._text


class FakePage:
    """Fake Playwright page shaped for AmazonAuthManager._classify_page."""

    def __init__(
        self,
        url: str = "https://www.amazon.com/",
        *,
        title: str = "Amazon.com",
        selectors: dict[str, FakeEl | None] | None = None,
        body_text: str = "Shop deals",
    ) -> None:
        self.url = url
        self._title = title
        self._selectors = selectors or {}
        self._body_text = body_text

    async def title(self) -> str:
        return self._title

    async def query_selector(self, selector: str) -> FakeEl | None:
        return self._selectors.get(selector)

    async def inner_text(self, selector: str) -> str:
        if selector == "body":
            return self._body_text
        el = self._selectors.get(selector)
        return await el.inner_text() if el else ""


def _mgr() -> AmazonAuthManager:
    # _classify_page only touches page + marketplace args; deps stay unused.
    return AmazonAuthManager.__new__(AmazonAuthManager)


@pytest.mark.asyncio
async def test_signed_out_bare_hello_is_not_confirmed():
    """The live-probed regression: bare "Hello" on the signed-out homepage
    must NOT confirm (old code confirmed here — a fake signed-in chip)."""
    page = FakePage(
        selectors={
            "#nav-link-accountList-nav-line-1": FakeEl("Hello"),
            "a[href*='/ap/signin']": FakeEl("Sign in"),
        }
    )
    check = await _mgr()._classify_page(page, US)
    assert check.status == AUTH_LOGIN_REQUIRED


@pytest.mark.asyncio
async def test_hello_sign_in_label_is_not_confirmed():
    page = FakePage(
        selectors={"#nav-link-accountList-nav-line-1": FakeEl("Hello, sign in")}
    )
    check = await _mgr()._classify_page(page, US)
    assert check.status == AUTH_LOGIN_REQUIRED


@pytest.mark.asyncio
async def test_named_hello_confirms():
    page = FakePage(
        selectors={"#nav-link-accountList-nav-line-1": FakeEl("Hello, Dana")}
    )
    check = await _mgr()._classify_page(page, US)
    assert check.status == AUTH_CONFIRMED
    assert check.detail.get("account_label") == "Hello, Dana"


@pytest.mark.asyncio
async def test_mobile_drawer_named_hello_confirms():
    page = FakePage(
        selectors={
            "#hmenu-content": FakeEl("Hello, Dana\nBrowse Best Sellers"),
        }
    )
    check = await _mgr()._classify_page(page, US)
    assert check.status == AUTH_CONFIRMED
    assert check.detail.get("via") == "mobile_drawer"


@pytest.mark.asyncio
async def test_mobile_drawer_sign_in_does_not_confirm():
    page = FakePage(
        selectors={"#hmenu-content": FakeEl("Hello, sign in\nShop by category")}
    )
    check = await _mgr()._classify_page(page, US)
    assert check.status == AUTH_LOGIN_REQUIRED


@pytest.mark.asyncio
async def test_validatecaptcha_wall_is_captcha_not_unknown():
    """The datacenter-IP bot-wall interstitial ('Continue shopping') must
    surface as attention-needed (captcha), not a silent 'unknown'."""
    page = FakePage(
        url="https://www.amazon.com/errors_page/validateCaptcha",
        selectors={"form[action*='validateCaptcha']": FakeEl("")},
        body_text="Click the button below to continue shopping",
    )
    check = await _mgr()._classify_page(page, US)
    assert check.status == AUTH_CAPTCHA


@pytest.mark.asyncio
async def test_wall_by_body_text_also_captcha():
    page = FakePage(
        url="https://www.amazon.com/",
        body_text=(
            "Click the button below to continue shopping Continue shopping "
            "Conditions of Use Privacy Policy"
        ),
    )
    check = await _mgr()._classify_page(page, US)
    assert check.status == AUTH_CAPTCHA


@pytest.mark.asyncio
async def test_unrecognized_page_stays_unknown():
    page = FakePage(body_text="Totally unknown layout")
    check = await _mgr()._classify_page(page, US)
    assert check.status == AUTH_UNKNOWN
