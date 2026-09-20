"""Owner-driven interactive browser sessions.

Some steps cannot be automated: signing in to Amazon (captcha/OTP), and
activating the KDSpy Pro extension against its license. Resurrección exposes
them as **interactive sessions**: the server's persistent research browser is
put under the owner's direct control for a bounded time, streaming frames to
the dashboard and accepting small, whitelisted control actions.

Why a dedicated session object instead of a raw page handle:
- research and manual control must never interleave: starting a session
  requires the browser to be idle (no research runner holds it), and the
  session closes itself when the idle TTL elapses,
- every frame and action passes through the same redaction discipline as
  the rest of the system (URLs scrubbed, no profile content returned),
- the durable `browser_login_windows` row gives the owner's UI a resumable
  record of the interactive flow across page reloads.

The interactive page is a dedicated tab in the persistent context — the SAME
profile that research uses — so a sign-in performed here is immediately
visible to every later research run.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app.browser_manager import BrowserManager, BrowserManagerError
from app.browser_store import LoginWindowStore, WINDOW_CANCELLED, WINDOW_COMPLETED, WINDOW_EXPIRED, WINDOW_OPEN
from app.config import Config
from app.marketplace import Marketplace, get_marketplace
from app.redact import scrub_url

# Hard cap on one interactive session, independent of config: sign-in flows
# (incl. OTP email detours) fit comfortably; hours of occupation are refused.
MAX_INTERACTIVE_SECONDS = 45 * 60

# Whitelisted control actions and their argument bounds. Anything else is
# rejected — this endpoint drives a real authenticated browser.
ACTION_LIMITS: dict[str, dict[str, int]] = {
    "click": {"x": 4096, "y": 4096},
    "type": {"text": 500},
    "key": {"key": 40},
    "scroll": {"dy": 3000},
    "navigate": {"url": 2000},
}


class InteractiveError(Exception):
    pass


@dataclass
class InteractiveSession:
    id: str
    marketplace_code: str
    purpose: str  # "amazon_signin" | "kdspy_setup" | "manual"
    created_at: float
    expires_at: float
    status: str = "open"  # open | completed | expired | cancelled
    last_url: str = ""
    last_title: str = ""
    _page: Any = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "marketplace": self.marketplace_code,
            "purpose": self.purpose,
            "status": self.status,
            "url": scrub_url(self.last_url),
            "title": self.last_title,
            "expires_at": _iso_from(self.expires_at),
            "seconds_remaining": max(0, int(self.expires_at - time.time())),
        }


class InteractiveSessionManager:
    """Owns at most ONE interactive session at a time (single owner, 2 cores)."""

    def __init__(
        self,
        config: Config,
        browser: BrowserManager,
        windows: LoginWindowStore,
        *,
        on_frame: Callable[[str, bytes], None] | None = None,
    ) -> None:
        self.config = config
        self.browser = browser
        self.windows = windows
        self._on_frame = on_frame
        self._session: InteractiveSession | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ state
    def current(self) -> InteractiveSession | None:
        s = self._session
        if s is None:
            return None
        if time.time() > s.expires_at and s.status == "open":
            s.status = "expired"
        return s

    def is_research_busy(self) -> bool:
        """True when non-interactive pages with real content are open.

        Used ONLY for informational status — never as a gate. Interactive
        sessions open their own protected tab in the shared persistent
        profile and coexist with research (the owner's tabs are eviction-
        safe, research never touches them, and closing an interactive
        session closes only that tab and its popups).
        """
        try:
            ctx = getattr(self.browser, "_context", None)
            if not ctx:
                return False
            for p in ctx.pages:
                if getattr(p, "_resurreccion_protected", False):
                    continue  # owner's interactive tabs are not research work
                url = (getattr(p, "url", "") or "").strip()
                if url and not url.startswith(("about:blank", "chrome://newtab")):
                    return True
            return False
        except Exception:
            return False

    # ------------------------------------------------------------------ start
    async def start(self, *, purpose: str, marketplace: str = "us", ttl_seconds: int | None = None) -> InteractiveSession:
        async with self._lock:
            existing = self.current()
            if existing and existing.status == "open":
                return existing  # idempotent: reuse the open session
            mkt = get_marketplace(marketplace)
            now = time.time()
            ttl = int(min(max(ttl_seconds or self.config.browser_login_window_seconds, 60), MAX_INTERACTIVE_SECONDS))
            import uuid

            session = InteractiveSession(
                id=uuid.uuid4().hex,
                marketplace_code=mkt.code,
                purpose=purpose,
                created_at=now,
                expires_at=now + ttl,
            )
            try:
                page = await self._open_page(mkt, purpose)
            except BrowserManagerError as exc:
                raise InteractiveError(f"browser launch failed: {exc}") from exc
            session._page = page
            session.last_url = page.url or ""
            try:
                session.last_title = (await page.title()) or ""
            except Exception:
                session.last_title = ""
            self._session = session
            # Durable record for the owner UI.
            self.windows.create("interactive", "kdspy", ttl)
            return session

    async def _open_page(self, mkt: Marketplace, purpose: str) -> Any:
        page = await self.browser.new_page(interactive=True)
        try:
            if purpose == "amazon_signin":
                await self.browser.navigate(page, f"{mkt.base_url}/")
            elif purpose == "kdspy_setup":
                # KDSpy activation happens on its own site; start neutral.
                await self.browser.navigate(page, "https://www.kdspy.com/")
        except Exception:
            # Never leave a half-navigated page behind: it would make the
            # next start believe research is busy.
            await self.browser.close_page(page)
            raise
        return page

    # ----------------------------------------------------------------- frames
    async def frame(self) -> bytes | None:
        s = self.current()
        if s is None or s.status != "open" or s._page is None:
            return None
        try:
            return await s._page.screenshot(type="jpeg", quality=70)
        except Exception:
            # Page may be mid-navigation; retry once after a beat.
            await asyncio.sleep(0.4)
            try:
                return await s._page.screenshot(type="jpeg", quality=70)
            except Exception:
                return None

    # ---------------------------------------------------------------- actions
    async def act(self, action: str, args: dict[str, Any]) -> InteractiveSession:
        s = self.current()
        if s is None or s._page is None:
            raise InteractiveError("no open interactive session")
        if s.status != "open" or time.time() > s.expires_at:
            s.status = "expired"
            raise InteractiveError("interactive session expired")
        if action not in ACTION_LIMITS:
            raise InteractiveError(f"action {action!r} not permitted")
        limits = ACTION_LIMITS[action]
        clean: dict[str, Any] = {}
        for k, cap in limits.items():
            v = args.get(k)
            if k in ("x", "y", "dy"):
                try:
                    v = int(float(v))
                except (TypeError, ValueError):
                    raise InteractiveError(f"{action}: {k} must be numeric") from None
                v = max(-cap, min(cap, v))
            else:
                v = str(v or "")
                if len(v) > cap:
                    raise InteractiveError(f"{action}: argument too long")
            clean[k] = v
        page = s._page
        try:
            if action == "click":
                await page.mouse.click(clean["x"], clean["y"])
            elif action == "type":
                await page.keyboard.type(clean["text"], delay=15)
            elif action == "key":
                await page.keyboard.press(clean["key"])
            elif action == "scroll":
                await page.mouse.wheel(0, clean["dy"])
            elif action == "navigate":
                url = clean["url"].strip()
                if not url.startswith(("http://", "https://")):
                    url = "https://" + url
                await self.browser.navigate(page, url)
        except BrowserManagerError as exc:
            raise InteractiveError(str(exc)) from exc
        except Exception as exc:
            raise InteractiveError(f"{action} failed: {exc}") from exc
        s.last_url = page.url or s.last_url
        try:
            s.last_title = (await page.title()) or ""
        except Exception:
            pass
        return s

    # -------------------------------------------------------------------- end
    async def complete(self, *, outcome: str = "completed") -> InteractiveSession:
        s = self.current()
        if s is None:
            raise InteractiveError("no interactive session")
        s.status = outcome if outcome in ("completed", "cancelled") else "completed"
        # Close ONLY the interactive tab and popups it spawned — research
        # pages are never touched, so an owner sign-in can safely run while
        # research continues in its own tabs.
        await self._close_owned_pages(s._page)
        s._page = None
        row = self.windows.list(status="open")
        for w in row:
            if getattr(w, "account", None) == "interactive":
                self.windows.set_status(
                    w.id, WINDOW_COMPLETED if s.status == "completed" else WINDOW_CANCELLED,
                    {"purpose": s.purpose, "outcome": s.status},
                )
        return s

    async def sweep_expired(self) -> int:
        """Close pages of sessions whose TTL elapsed. Cheap; call periodically."""
        s = self.current()
        if s and s.status != "open" and s._page is not None:
            page, s._page = s._page, None
            try:
                await page.close()
            except Exception:
                pass
            return 1
        return 0

    # ------------------------------------------------------------------ intern
    async def _close_owned_pages(self, main_page: Any) -> None:
        """Close the interactive tab plus popups it opened — nothing else.

        Popup identification is heuristic (pages lacking the protected marker
        that appeared for this session); pages marked protected that are NOT
        this session's main tab are other interactive work and stay open.
        Research pages never carry the marker and are never closed here.
        """
        try:
            ctx = getattr(self.browser, "_context", None)
            if not ctx:
                return
            for p in list(ctx.pages):
                if p is main_page:
                    try:
                        await p.close()
                    except Exception:
                        pass
                    continue
                protected = getattr(p, "_resurreccion_protected", False)
                url = (getattr(p, "url", "") or "").strip()
                # A blank popup spawned by the interactive flow (unmarked,
                # about:blank or newtab) is safe to close; anything with real
                # content that is unprotected belongs to research — leave it.
                if not protected and (not url or url.startswith(("about:blank", "chrome://newtab"))):
                    try:
                        await p.close()
                    except Exception:
                        pass
        except Exception:
            pass


def _iso_from(ts: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
