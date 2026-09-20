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
    "switch_tab": {"index": 8},
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
    # Pages owned by this session, in open order. index 0 is the tab the
    # session started with; later entries are popups the site opened (e.g.
    # kdspy.com's Login button opens its member-login page in a NEW TAB).
    pages: list[Any] = field(default_factory=list)
    active: int = 0

    @property
    def page(self) -> Any:
        """The currently-viewed page (what frames and actions target)."""
        if not self.pages:
            return None
        return self.pages[min(self.active, len(self.pages) - 1)]

    @property
    def _page(self) -> Any:  # back-compat alias used by frame/act/complete
        return self.page

    def to_dict(self) -> dict[str, Any]:
        p = self.page
        return {
            "id": self.id,
            "marketplace": self.marketplace_code,
            "purpose": self.purpose,
            "status": self.status,
            "url": scrub_url(getattr(p, "url", "") or self.last_url),
            "title": self.last_title,
            "expires_at": _iso_from(self.expires_at),
            "seconds_remaining": max(0, int(self.expires_at - time.time())),
            "tabs": [scrub_url(getattr(x, "url", "") or "") for x in self.pages],
            "active_tab": self.active,
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
            session.pages = [page]
            session.active = 0
            self._watch_popups(session, page)
            session.last_url = page.url or ""
            try:
                session.last_title = (await page.title()) or ""
            except Exception:
                session.last_title = ""
            self._session = session
            # Durable record for the owner UI.
            self.windows.create("interactive", "kdspy", ttl)
            return session

    def _watch_popups(self, session: InteractiveSession, page: Any) -> None:
        """Track tabs the site opens (target=_blank / window.open).

        kdspy.com's Login opens its member-login form in a NEW tab — without
        this, the owner taps the button, the stream keeps showing the old
        page, and the login form sits invisible in a hidden tab. Every popup
        becomes a switchable tab, auto-focuses, and joins the session so
        complete() closes it too.
        """
        def _on_popup(popup: Any) -> None:
            if session.status != "open":
                return
            if popup not in session.pages:
                session.pages.append(popup)
                session.active = len(session.pages) - 1  # auto-focus the new tab
                self._watch_popups(session, popup)
        try:
            page.on("popup", _on_popup)
        except Exception:
            pass

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
            return await s._page.screenshot(type="jpeg", quality=62)
        except Exception:
            # Page may be mid-navigation; retry once after a beat.
            await asyncio.sleep(0.4)
            try:
                return await s._page.screenshot(type="jpeg", quality=62)
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
            elif k == "index":  # non-negative int — 0 is valid, never falsy-coerce
                try:
                    v = max(0, min(int(float(v)), cap))
                except (TypeError, ValueError):
                    raise InteractiveError(f"{action}: {k} must be numeric") from None
            else:
                v = str(v if v is not None else "")
                if len(v) > cap:
                    raise InteractiveError(f"{action}: argument too long")
            clean[k] = v
        page = s.page
        if page is None:
            raise InteractiveError("no open interactive session")
        try:
            if action == "switch_tab":
                idx = max(0, min(int(clean["index"]), len(s.pages) - 1))
                s.active = idx
                page = s.page
            elif action == "click":
                await self._click_snapped(page, clean["x"], clean["y"])
                # A click may have opened a new tab; focus it when it did.
                if s.pages and s.pages[-1] is not page and len(s.pages) > s.active + 1:
                    s.active = len(s.pages) - 1
                    page = s.page
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
    async def _click_snapped(self, page: Any, x: int, y: int) -> None:
        """Tap-to-click with fat-finger tolerance.

        The owner taps a 1440px-wide page rendered on a ~390px phone screen
        (≈3.7x compression, ±10px finger accuracy ≈ ±37 page px). A raw
        pixel click misses small targets constantly. So: resolve the element
        under the tapped point, and when it sits inside something genuinely
        interactive (a/button/input/label/…), click the CENTER of that
        element instead — the whole control becomes the target. Plain text
        taps stay pixel-exact.
        """
        snapped: dict[str, Any] | None = None
        try:
            snapped = await page.evaluate(
                """([x, y]) => {
                    const INTERACTIVE = 'a, button, [role=button], input, select, textarea, label, summary, [onclick], [jsaction]';
                    const pick = (el) => {
                        if (!el || !el.closest) return null;
                        const t = el.closest(INTERACTIVE);
                        if (!t) return null;
                        const r = t.getBoundingClientRect();
                        if (!r || r.width <= 0 || r.height <= 0) return null;
                        if (r.width > 700 || r.height > 400) return null; // container, not a control
                        return { interactive: true, x: r.left + r.width / 2, y: r.top + r.height / 2, area: r.width * r.height };
                    };
                    const direct = pick(document.elementFromPoint(x, y));
                    if (direct) return direct;
                    // Fat-finger recovery: the phone renders a 1440px page at
                    // ~0.27x, so a 20px-tall link is ~5px on screen — taps a
                    // few pixels off land on dead space and feel ignored.
                    // Probe outward in rings; the first ring containing a real
                    // control wins (closest ring = nearest), smallest control
                    // preferred so we hit the link, not its whole navbar.
                    for (const rad of [14, 28, 42, 56]) {
                        let best = null;
                        for (let a = 0; a < 8; a++) {
                            const th = (Math.PI / 4) * a;
                            const cand = pick(document.elementFromPoint(x + rad * Math.cos(th), y + rad * Math.sin(th)));
                            if (!cand) continue;
                            const d = Math.hypot(cand.x - x, cand.y - y);
                            const score = d + Math.sqrt(cand.area) / 8;
                            if (!best || score < best.score) best = { ...cand, score };
                        }
                        if (best) return best;
                    }
                    return { interactive: false };
                }""",
                [x, y],
            )
        except Exception:
            snapped = None  # mid-navigation, CSP, about:blank — raw click
        if isinstance(snapped, dict) and snapped.get("interactive"):
            try:
                await page.mouse.click(float(snapped["x"]), float(snapped["y"]))
                return
            except Exception:
                pass  # fall through to the raw pixel click
        await page.mouse.click(x, y)

    async def complete(self, *, outcome: str = "completed") -> InteractiveSession:
        s = self.current()
        if s is None:
            raise InteractiveError("no interactive session")
        s.status = outcome if outcome in ("completed", "cancelled") else "completed"
        # Close ONLY this session's tabs — the main tab plus every popup it
        # opened. Research pages are never touched, so an owner sign-in can
        # safely run while research continues in its own tabs.
        for p in list(s.pages):
            try:
                await p.close()
            except Exception:
                pass
        s.pages = []
        s.active = 0
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
        if s and s.status != "open" and s.pages:
            for p in list(s.pages):
                try:
                    await p.close()
                except Exception:
                    pass
            s.pages = []
            s.active = 0
            return 1
        return 0


def _iso_from(ts: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
