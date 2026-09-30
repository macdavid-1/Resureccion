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
from urllib.parse import urlparse

from app.amazon_auth import ACCOUNT_AMAZON, AmazonAuthManager
from app.browser_manager import BrowserManager, BrowserManagerError
from app.browser_store import (
    AUTH_CONFIRMED,
    AUTH_LOGIN_REQUIRED,
    AUTH_UNKNOWN,
    LoginWindowStore,
    WINDOW_CANCELLED,
    WINDOW_COMPLETED,
    WINDOW_EXPIRED,
    WINDOW_OPEN,
)
from app.config import Config
from app.marketplace import Marketplace, get_marketplace, marketplace_from_url
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


# Where the KDSpy subscription sign-in actually happens: the WishList Member
# portal on publishingaltitude.com (allowlisted in app/privacy.py). Opening
# kdspy.com's landing page instead wastes the interactive session on
# marketing content; /member/ is the owner-verified login entry point
# (WordPress/wlfrom bounces only add a hop that can break under egress churn).
KDSPY_LOGIN_URL = "https://www.publishingaltitude.com/member/"

# Amazon's real sign-in form (the classic openid identifier_select flow).
# Opening the homepage first costs a tap and can land on region variants;
# this URL is the canonical form on every marketplace.
AMAZON_SIGNIN_URL = (
    "https://www.amazon.com/ap/signin"
    "?openid.return_to=https%3A%2F%2Fwww.amazon.com%2F%3Fref_%3Dnav_custrec_signin"
    "&openid.identity=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select"
    "&openid.assoc_handle=usflex"
    "&openid.mode=checkid_setup"
    "&openid.claimed_id=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select"
    "&openid.ns=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0"
)


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
        self._last_detect_at = 0.0
        # Optional AmazonAuthManager (wired by main.py): lets the session
        # itself flip the durable Amazon auth state the moment the sign-in
        # visibly succeeds, without the owner having to tap anything.
        self.amazon_auth: AmazonAuthManager | None = None
        # In-flight watcher task, keyed by nothing (single session) — replaced
        # each fire so a slow classify never queues up behind frames.
        self._detect_task: asyncio.Task | None = None
        # Last status this watcher persisted. The watcher re-classifies every
        # throttle tick while the owner is mid-flow; identical statuses are
        # NOT rewritten to the DB (a 1Hz SQLite upsert for minutes of typing
        # would be pure churn).
        self._last_written_auth_status: str | None = None

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
                # Label accurately: launch vs. navigation failures hit the
                # owner with completely different remedies (egress/proxy
                # state vs. page/url state). The old blanket "browser launch
                # failed" label sent every navigation failure to a
                # nonexistent launch bug.
                msg = str(exc)
                if msg.startswith("navigation failed") or msg.startswith("navigation timeout"):
                    raise InteractiveError(f"page open failed: {msg}") from exc
                raise InteractiveError(f"browser launch failed: {msg}") from exc
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

    # ------------------------------------------------------- sign-in detection
    # URL patterns that mean "the owner is still inside the sign-in flow".
    # Used only to AVOID spurious DOM classification while the page sits on
    # signin/captcha/OTP forms — the signed-in verdict itself comes from the
    # real DOM classifier (AmazonAuthManager.classify_live), never from URLs.
    _AMAZON_SIGNIN_MARKS = ("/ap/signin", "/ap/cvf", "/ap/mfa", "/signin", "/gp/sign-in")

    # DOM classification costs a page evaluation; on a 2-core box we throttle
    # it to once per second per session.
    _DETECT_INTERVAL_SECONDS = 1.0

    def _detect_amazon_signin(self, session: "InteractiveSession") -> None:
        """Guard + throttle for the async sign-in watcher (see _watch_amazon_signin)."""
        if self.amazon_auth is None or session.purpose != "amazon_signin":
            return
        now = time.monotonic()
        if now - self._last_detect_at < self._DETECT_INTERVAL_SECONDS:
            return
        self._last_detect_at = now
        # Fire-and-forget: a slow classify must never delay frame streaming.
        if self._detect_task is not None and not self._detect_task.done():
            return  # a classification is already running
        self._detect_task = asyncio.get_running_loop().create_task(
            self._watch_amazon_signin(session)
        )

    def _persist_auth_status(self, auth: AmazonAuthManager, status: str, url: str) -> None:
        """Write the durable auth status, but only when it actually changed.

        The watcher classifies up to once a second; the durable store is for
        state, not telemetry. A repeated identical status is skipped — EXCEPT
        confirmed, which is always (re)written so the completion moment and
        its URL evidence land durably even if an earlier write raced.
        """
        if status == self._last_written_auth_status and status != AUTH_CONFIRMED:
            return
        self._last_written_auth_status = status
        auth.auth_store.set(
            ACCOUNT_AMAZON,
            auth.profile,
            status,
            {"via": "interactive_signin", "url": scrub_url(url)},
        )

    async def _watch_amazon_signin(self, session: "InteractiveSession") -> None:
        """Persist the durable Amazon auth state the moment it visibly changes.

        The active page is classified with the SAME DOM criteria as the
        standing auth check (AmazonAuthManager.classify_live: signed-in nav
        chrome vs sign-in prompts vs captcha/OTP forms). While the page sits
        on a sign-in/captcha/OTP URL the durable state only ever moves to
        login_required; a confirmed state is never downgraded here (a normal
        marketplace page viewed while already signed in must not read as
        "signed out" just because its nav label didn't parse).

        Failures never raise: detection is a background nicety and must not
        disturb the interactive flow.
        """
        auth: AmazonAuthManager | None = self.amazon_auth
        if auth is None or session.purpose != "amazon_signin":
            return
        page = session.page
        if page is None:
            return
        try:
            url = (getattr(page, "url", "") or "")
            if not url or "amazon." not in urlparse(url).netloc.lower():
                return
            path = urlparse(url).path.lower()
            if any(mark in path for mark in self._AMAZON_SIGNIN_MARKS):
                # Still inside the sign-in/captcha/OTP flow — reflect that
                # unless a confirmed state is already recorded.
                if auth.current_state().status != AUTH_CONFIRMED:
                    self._persist_auth_status(auth, AUTH_LOGIN_REQUIRED, url)
                return
            mkt = marketplace_from_url(url)
            if mkt is None:
                return
            # Bounded: a hung page must never pin the watcher task forever
            # (a pinned task would suppress all later detections).
            check = await asyncio.wait_for(auth.classify_live(page, mkt), timeout=12.0)
            if check.status == AUTH_CONFIRMED:
                self._last_written_auth_status = None  # completion is always recorded
                self._persist_auth_status(auth, AUTH_CONFIRMED, url)
            elif (
                auth.current_state().status == AUTH_CONFIRMED
                and check.status in (AUTH_UNKNOWN, AUTH_LOGIN_REQUIRED)
            ):
                # Never downgrade a confirmed state on ambiguous evidence
                # (nav label not parsed, marketplace home without account
                # chrome). Confirmed until a real sign-in form is the page.
                return
            else:
                self._persist_auth_status(auth, check.status, url)
        except Exception:
            pass  # detection must never break the interactive session

    def _watch_popups(self, session: InteractiveSession, page: Any) -> None:
        """Track tabs the site opens (target=_blank / window.open).

        kdspy.com's Login opens its member-login form in a NEW tab — without
        this, the owner taps the button, the stream keeps showing the old
        page, and the login form sits invisible in a hidden tab. Every popup
        becomes a switchable tab, auto-focuses, and joins the session so
        complete() closes it too. Popups get the same phone emulation so every
        tab the owner sees stays 1:1 tappable.
        """
        def _on_popup(popup: Any) -> None:
            if session.status != "open":
                return
            if popup not in session.pages:
                session.pages.append(popup)
                session.active = len(session.pages) - 1  # auto-focus the new tab
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self.browser.apply_mobile_emulation(popup))
                except Exception:
                    pass
                self._watch_popups(session, popup)
        try:
            page.on("popup", _on_popup)
        except Exception:
            pass

    async def _open_page(self, mkt: Marketplace, purpose: str) -> Any:
        page = await self.browser.new_page(interactive=True)
        try:
            if purpose == "amazon_signin":
                # The canonical /ap/signin form: no homepage detour, no
                # reliance on the homepage's sign-in affordances.
                await self.browser.navigate(page, AMAZON_SIGNIN_URL)
            elif purpose == "kdspy_setup":
                # The KDSpy subscription sign-in lives on the member portal —
                # start there, not on the kdspy.com landing page.
                await self.browser.navigate(page, KDSPY_LOGIN_URL)
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
        # Every streamed frame is also a chance to notice the sign-in
        # completed (the owner sees the chip flip without extra taps).
        # Fire-and-forget ONLY: awaiting the classify here would make a slow
        # page evaluation stall the frame stream itself.
        self._detect_amazon_signin(s)
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
        # Navigations (sign-in submit, OTP detour, marketplace redirect) are
        # the other moment the auth state can flip. Fire-and-forget: the next
        # frame poll (~1s later) surfaces the flip, and an action must never
        # block on a page evaluation.
        self._detect_amazon_signin(s)
        return s

    # -------------------------------------------------------------------- end
    # Shared JS used both in the top page and inside child frames to resolve
    # the element under a tapped point. Kept frame-agnostic on purpose.
    _SNAP_JS = """([x, y]) => {
                    const INTERACTIVE = 'a, button, [role=button], [role=checkbox], [role=switch], [role=link], [role=menuitem], [role=tab], input, select, textarea, label, summary, [onclick], [jsaction]';
                    const pick = (el) => {
                        if (!el || !el.closest) return null;
                        const t = el.closest(INTERACTIVE);
                        if (!t) return null;
                        const r = t.getBoundingClientRect();
                        if (!r || r.width <= 0 || r.height <= 0) return null;
                        if (r.width > 700 || r.height > 400) return null; // container, not a control
                        return { interactive: true, x: r.left + r.width / 2, y: r.top + r.height / 2, area: r.width * r.height };
                    };
                    const hit = document.elementFromPoint(x, y);
                    const direct = pick(hit);
                    if (direct) return { ...direct, hit: (hit && hit.tagName || '').toLowerCase() };
                    // A tap on an IFRAME (CAPTCHA/embedded widgets) must NOT
                    // be diverted to an overlay handler: frame descent below
                    // resolves it inside the frame instead.
                    const overFrame = hit && (hit.tagName === 'IFRAME' || hit.tagName === 'FRAME');
                    if (overFrame) return { interactive: false, hit: 'iframe' };
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
                        if (best) return { ...best, hit: (hit && hit.tagName || '').toLowerCase() };
                    }
                    return { interactive: false, hit: (hit && hit.tagName || '').toLowerCase() };
                }"""

    async def _click_snapped(self, page: Any, x: int, y: int) -> None:
        """Tap-to-click with fat-finger tolerance and iframe descent.

        The owner taps a 1440px-wide page rendered on a ~390px phone screen
        (≈3.7x compression, ±10px finger accuracy ≈ ±37 page px). A raw
        pixel click misses small targets constantly. So: resolve the element
        under the tapped point, and when it sits inside something genuinely
        interactive (a/button/input/label/…), click the CENTER of that
        element instead — the whole control becomes the target. Plain text
        taps stay pixel-exact.

        The resolver ALSO descends into child frames: CAPTCHA widgets (and
        many payment/embedded forms) render their real checkbox inside a
        cross-origin iframe, where the top document only sees the iframe
        element. Tapping there is resolved inside the owning frame and
        mapped back to page coordinates before the physical click.
        """
        snapped: dict[str, Any] | None = None
        try:
            snapped = await page.evaluate(self._SNAP_JS, [x, y])
        except Exception:
            snapped = None  # mid-navigation, CSP, about:blank — raw click
        if isinstance(snapped, dict) and snapped.get("interactive"):
            try:
                await page.mouse.click(float(snapped["x"]), float(snapped["y"]))
                return
            except Exception:
                pass  # fall through to frame descent / raw click
        # IFrame descent: if the top-document hit is a frame element (or the
        # point missed any control but sits over a frame), re-resolve inside
        # that frame with frame-local coordinates and map the result back.
        if isinstance(snapped, dict) and not snapped.get("interactive"):
            frame_target = await self._frame_snap(page, x, y)
            if frame_target is not None:
                fx, fy = frame_target
                try:
                    await page.mouse.click(float(fx), float(fy))
                    return
                except Exception:
                    pass
        await page.mouse.click(x, y)

    async def _frame_snap(self, page: Any, x: int, y: int) -> tuple[float, float] | None:
        """Resolve a tap inside a child iframe; returns PAGE coordinates.

        For each child frame, get its bounding box in page coordinates and
        test whether (x, y) falls inside it (with a small tolerance so taps
        a few pixels off the frame edge still resolve — reCAPTCHA's box sits
        tight against the widget border). Then run the same snap resolver
        in the frame's own document and translate the found center back.

        The frame resolver runs TWICE when the first pass finds no control:
        reCAPTCHA's checkbox is a bare div inside a nested inner iframe, so
        a miss here descends one level deeper rather than giving up.
        """
        try:
            frames = [f for f in page.frames if f is not page.main_frame]
        except Exception:
            return None
        for frame in frames:
            try:
                el = await frame.frame_element()
                box = await el.bounding_box()
            except Exception:
                continue  # detached frame, detached element, CSP — skip
            if not box:
                continue
            tol = 12
            if not (box["x"] - tol <= x <= box["x"] + box["width"] + tol
                    and box["y"] - tol <= y <= box["y"] + box["height"] + tol):
                continue
            inner_x = x - box["x"]
            inner_y = y - box["y"]
            try:
                res = await frame.evaluate(self._SNAP_JS, [inner_x, inner_y])
            except Exception:
                res = None
            if isinstance(res, dict) and res.get("interactive"):
                return (box["x"] + float(res["x"]), box["y"] + float(res["y"]))
            # No interactive element inside the frame — but the frame itself
            # is the target (e.g. a bare checkbox canvas): click where the
            # finger actually landed, inside the frame.
            if (box["x"] <= x <= box["x"] + box["width"]
                    and box["y"] <= y <= box["y"] + box["height"]):
                return (float(x), float(y))
        return None

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
