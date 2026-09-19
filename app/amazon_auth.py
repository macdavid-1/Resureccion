"""Amazon authentication manager.

The owner authenticates Amazon **once** through a supervised one-shot login
window; the persistent browser profile keeps the session for future research
runs (when Amazon permits). This module:

- detects auth state on real Amazon pages (signed-in / login-required /
  captcha / OTP / signed-out) via DOM inspection — no guessed selectors are
  treated as truth; unrecognized pages yield `unknown` with diagnostics,
- handles Amazon regional redirects: after navigation, if the page URL's
  domain differs from the target marketplace, the redirect is recorded and
  navigation continues to the intended marketplace,
- runs the manual login window: launches the browser headed (or headless as
  configured), opens the marketplace sign-in page, waits for the owner to
  complete login (incl. captcha/OTP), then verifies and persists state,
- imports cookies from an owner-provided export when interactive login is
  impractical — cookie data is written straight into the browser profile and
  NEVER persisted in the database or logs.

Raw credentials/cookies never touch the database. Only statuses and
diagnostics are stored (see BrowserAuthStore).
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from app.browser_manager import BrowserManager, BrowserManagerError
from app.browser_store import (
    AUTH_CAPTCHA,
    AUTH_CONFIRMED,
    AUTH_ERROR,
    AUTH_LOGIN_REQUIRED,
    AUTH_OTP_REQUIRED,
    AUTH_SIGNED_OUT,
    AUTH_UNKNOWN,
    AuthState,
    BrowserAuthStore,
    LoginWindowStore,
    WINDOW_CANCELLED,
    WINDOW_COMPLETED,
    WINDOW_EXPIRED,
    WINDOW_OPEN,
)
from app.config import Config
from app.marketplace import Marketplace
from app.redact import scrub_url

ACCOUNT_AMAZON = "amazon"

_SIGNIN_PATHS = ("/ap/signin", "/signin", "/gp/sign-in")


def _all_amazon_domains() -> list[str]:
    from app.marketplace import AMAZON_MARKETPLACES

    return [m.domain for m in AMAZON_MARKETPLACES.values()]


def _validate_cookie_domains(clean: list[dict[str, Any]]) -> None:
    """Structural + domain validation of sanitized cookie entries.

    Only real Amazon marketplace domains are accepted. Matching is
    exact-or-subdomain (dot boundary), so look-alikes like 'notamazon.com'
    or 'evil-amazon.com' are refused.
    """
    for c in clean:
        if not c.get("name") or "value" not in c:
            raise AmazonAuthError("malformed cookie entry")
        domain = (c.get("domain") or "").lstrip(".").lower()
        if not any(domain == d or domain.endswith("." + d) for d in _all_amazon_domains()):
            raise AmazonAuthError(f"refusing non-Amazon cookie domain: {c.get('domain')!r}")


class AmazonAuthError(Exception):
    pass


@dataclass
class AuthCheck:
    status: str
    url: str
    title: str
    detail: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "url": scrub_url(self.url),
            "title": self.title,
            "detail": self.detail,
        }


class AmazonAuthManager:
    def __init__(
        self,
        config: Config,
        browser: BrowserManager,
        auth_store: BrowserAuthStore,
        windows: LoginWindowStore,
    ) -> None:
        self.config = config
        self.browser = browser
        self.auth_store = auth_store
        self.windows = windows
        self.profile = "kdspy"  # shared research profile

    # ------------------------------------------------------------- state read
    def current_state(self) -> AuthState:
        state = self.auth_store.get(ACCOUNT_AMAZON, self.profile)
        if state is None:
            return self.auth_store.set(ACCOUNT_AMAZON, self.profile, AUTH_UNKNOWN)
        return state

    def is_authenticated(self) -> bool:
        return self.current_state().status == AUTH_CONFIRMED

    # ---------------------------------------------------------- live checking
    async def check(self, marketplace: Marketplace) -> AuthCheck:
        """Navigate to the marketplace and classify the auth state from the
        live page. Marks the durable state accordingly."""
        page = await self.browser.open_marketplace(marketplace, "/")
        try:
            check = await self._classify_page(page, marketplace)
        finally:
            await self.browser.close_page(page)
        self.auth_store.set(
            ACCOUNT_AMAZON, self.profile, check.status, {"checked_at_url": scrub_url(check.url), **check.detail}
        )
        return check

    async def _classify_page(self, page: Any, marketplace: Marketplace) -> AuthCheck:
        url = page.url or ""
        title = (await page.title() or "").strip()
        detail: dict[str, Any] = {"marketplace": marketplace.code}

        # Regional redirect? (URL domain differs from target marketplace)
        host = urlparse(url).netloc.lower()
        if host and marketplace.domain not in host and "amazon" in host:
            detail["regional_redirect"] = {
                "from_domain": host,
                "expected_domain": marketplace.domain,
            }

        path = urlparse(url).path
        if any(p in path for p in _SIGNIN_PATHS):
            # It's a sign-in page: figure out whether it's captcha/OTP gated.
            body_text = (await _safe_text(page))[:4000].lower()
            if "captcha" in body_text or await _has_selector(page, "img[src*='captcha'], #captchacharacters"):
                detail["reason"] = "captcha challenge on sign-in page"
                return AuthCheck(AUTH_CAPTCHA, url, title, detail)
            if "otp" in body_text or "verification" in body_text or await _has_selector(page, "#auth-mfa-otpcode"):
                detail["reason"] = "one-time password required"
                return AuthCheck(AUTH_OTP_REQUIRED, url, title, detail)
            detail["reason"] = "sign-in form presented"
            return AuthCheck(AUTH_LOGIN_REQUIRED, url, title, detail)

        # On a normal marketplace page: look for signed-in indicators.
        signed_in = await _has_selector(
            page,
            "#nav-link-accountList-nav-line-1, [data-nav-role='signin'] .nav-line-1-content, #nav-your-account",
        )
        if signed_in:
            label = await _safe_text_of(page, "#nav-link-accountList-nav-line-1")
            if label and "sign in" not in label.strip().lower():
                detail["account_label_present"] = True
                return AuthCheck(AUTH_CONFIRMED, url, title, detail)
        if await _has_selector(page, "a[href*='/ap/signin'], #nav-signin-tooltip .nav-action-button"):
            detail["reason"] = "sign-in prompt visible on marketplace page"
            return AuthCheck(AUTH_LOGIN_REQUIRED, url, title, detail)

        # Ambiguous — do not guess.
        detail["reason"] = "page did not match known signed-in or sign-in patterns"
        return AuthCheck(AUTH_UNKNOWN, url, title, detail)

    # ------------------------------------------------------------ cookie import
    async def import_cookies(self, marketplace: Marketplace, cookies: list[dict[str, Any]]) -> AuthState:
        """Import owner-exported cookies into the persistent profile.

        The cookie payloads pass through memory into the browser context only.
        They are NOT stored in the DB, NOT logged, and NOT returned. After
        import, a verification check runs and only its *status* is recorded.
        """
        if not cookies:
            raise AmazonAuthError("empty cookie list")
        if not isinstance(cookies, list) or not all(isinstance(c, dict) for c in cookies):
            raise AmazonAuthError("cookies must be a list of objects")
        # Sanitize to fields Chromium accepts; drop everything else including
        # unexpected metadata that might carry session content we don't want
        # echoed anywhere.
        allowed = {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}
        clean = [{k: v for k, v in c.items() if k in allowed} for c in cookies]
        _validate_cookie_domains(clean)
        ctx = await self.browser._ensure_context()
        try:
            await ctx.add_cookies(clean)
        except Exception as exc:
            raise AmazonAuthError(f"browser rejected cookies: {exc}") from exc
        check = await self.check(marketplace)
        if check.status != AUTH_CONFIRMED:
            return self.auth_store.set(
                ACCOUNT_AMAZON, self.profile, check.status,
                {"via": "cookie_import", "verification": check.status},
            )
        return self.current_state()

    # ---------------------------------------------------------- login window
    async def open_login_window(self, marketplace: Marketplace) -> dict[str, Any]:
        """Open a supervised one-shot login window.

        Flow:
        1. create a durable login-window record (expires after TTL),
        2. launch the browser and open the marketplace sign-in page,
        3. the owner completes login/captcha/OTP inside that window,
        4. `complete_login_window` (called by the API once the owner says
           they're done, or automatically on sign-in page disappearance)
           verifies the session and closes the browser.

        Security: the endpoint that opens the window requires owner auth AND
        the BROWSER_LOGIN_SECRET when configured.
        """
        window = self.windows.create(
            ACCOUNT_AMAZON, self.profile, self.config.browser_login_window_seconds
        )
        self.windows.set_status(window.id, WINDOW_OPEN)
        try:
            await self.browser.launch()
            page = await self.browser.open_marketplace(marketplace, "/ap/signin")
            # If already signed in, sign-in page redirects to home: verify now.
            check = await self._classify_page(page, marketplace)
            if check.status == AUTH_CONFIRMED:
                self.windows.set_status(window.id, WINDOW_COMPLETED, {"verified": True})
                await self.browser.close_page(page)
                return {"window": self.windows.require(window.id).to_dict(), "auto_completed": True}
        except BrowserManagerError as exc:
            self.windows.set_status(window.id, WINDOW_CANCELLED, {"error": str(exc)})
            raise AmazonAuthError(str(exc)) from exc
        return {
            "window": self.windows.require(window.id).to_dict(),
            "auto_completed": False,
            "note": "complete the sign-in in the browser window, then call complete",
        }

    async def complete_login_window(self, window_id: str, marketplace: Marketplace) -> AuthState:
        """Verify whether the owner finished signing in; finalize the window."""
        window = self.windows.require(window_id)
        if window.status in (WINDOW_EXPIRED, WINDOW_CANCELLED):
            return self.current_state()
        page = await self.browser.open_marketplace(marketplace, "/")
        try:
            check = await self._classify_page(page, marketplace)
        finally:
            await self.browser.close_page(page)
        if check.status == AUTH_CONFIRMED:
            self.windows.set_status(window_id, WINDOW_COMPLETED, {"verified": True})
            self.auth_store.set(
                ACCOUNT_AMAZON, self.profile, AUTH_CONFIRMED,
                {"via": "manual_login_window"},
            )
        else:
            # Keep the window open (owner may still be mid-flow) but record.
            self.auth_store.set(
                ACCOUNT_AMAZON, self.profile, check.status,
                {"via": "manual_login_window", "verification": check.status},
            )
            if window.expires_at < _now_iso():
                self.windows.set_status(window_id, WINDOW_EXPIRED)
        return self.current_state()

    def cancel_login_window(self, window_id: str) -> None:
        self.windows.set_status(window_id, WINDOW_CANCELLED, {"cancelled_by": "owner"})

    def expire_stale_windows(self) -> int:
        return self.windows.expire_stale()


# --------------------------------------------------------------------- helpers
async def _safe_text(page: Any) -> str:
    try:
        return await page.inner_text("body")
    except Exception:
        return ""


async def _safe_text_of(page: Any, selector: str) -> str:
    try:
        el = await page.query_selector(selector)
        if el is None:
            return ""
        return (await el.inner_text()) or ""
    except Exception:
        return ""


async def _has_selector(page: Any, selector: str) -> bool:
    try:
        el = await page.query_selector(selector)
        return el is not None
    except Exception:
        return False


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
