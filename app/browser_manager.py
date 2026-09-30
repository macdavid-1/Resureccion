"""Camoufox persistent browser manager.

Operates ONE real, persistent browser for the whole app (the 2-core/16GB
environment cannot afford a browser per action). The engine is Camoufox — the
anti-detect Firefox fork — and nothing else: no Chromium launch path and no
engine fallback exists. Camoufox is chosen because fingerprint consistency
(canvas/audio/fonts/WebGL/UA) is enforced natively by the browser binary, so
the research identity never depends on JS patches that anti-bot stacks see
through. The browser:

- uses a persistent user-data dir (`browser_profiles/kdspy`) so cookies,
  localStorage, and extension state survive restarts — this is how the
  owner's Amazon/KDSpy authentication is reused across research runs,
- loads the KDSpy *Firefox add-on* natively via Camoufox's ``addons`` launch
  option (installed one-tap from Mozilla Add-ons or by XPI upload),
- is launched lazily on first use and shut down after an idle period,
- tracks crashes and restarts itself cleanly.

Launch options live in app/camoufox_engine.py; this module owns the lifecycle.

This module is the ONLY place that touches playwright APIs. Credentials,
cookies, and storage state never leave here except into the profile dir.

The playwright import is deferred so the app (and tests) run on machines
without playwright installed — every public API raises `BrowserManagerError`
with a clear message in that case, and the rest of Resurrección works.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app import camoufox_engine
from app.browser_store import BrowserEvidenceStore
from app.config import Config
from app.kdspy import KDSpyManager
from app.marketplace import Marketplace, get_marketplace
from app.redact import scrub_url
from app import privacy
from app.proxy_spec import (
    is_proxy_nav_error,
    probe_proxy,
)

try:  # pragma: no cover - import guard
    from playwright.async_api import (
        BrowserContext,
        Error as PlaywrightError,
        Page,
        Playwright,
        TimeoutError as PlaywrightTimeoutError,
        async_playwright,
    )
    PLAYWRIGHT_AVAILABLE = True
except ImportError:  # pragma: no cover
    PLAYWRIGHT_AVAILABLE = False
    BrowserContext = Any  # type: ignore[misc,assignment]
    Page = Any  # type: ignore[misc,assignment]
    PlaywrightError = Exception  # type: ignore[misc,assignment]
    PlaywrightTimeoutError = Exception  # type: ignore[misc,assignment]


class BrowserManagerError(Exception):
    pass


# Consecutive browser-level proxy refusals (no successful navigation in
# between) that force egress to direct despite a passing probe. Two is the
# minimum that proves the refusal is not a one-off blip (one refusal is the
# transient retry's job) — and it is exactly the shape of the incident where
# a rate-limited CONNECT bridge answered every probe but refused every
# browser tunnel for days, surfacing NS_ERROR_PROXY_CONNECTION_REFUSED to
# the owner on both the KDSpy and Amazon sign-in URLs.
_PROXY_REFUSAL_ESCALATE_AFTER = 2
# Refusals further apart than this are not "consecutive" — the streak resets
# so a single refusal hours ago cannot ambush an unrelated navigation.
_PROXY_REFUSAL_STREAK_WINDOW = 120.0
# Hard cap on goto attempts within one navigation call. The escalation
# ladder can request at most three retries (dead-proxy recovery, the one
# transient retry, the forced demotion); this is the defensive guarantee.
_MAX_NAV_ATTEMPTS = 4


@dataclass
class BrowserStatus:
    running: bool
    headless: bool
    profile_dir: str
    kdspy_loaded: bool
    open_pages: int
    last_launched_at: str | None
    last_activity_at: str | None
    launch_error: str | None
    crash_count: int
    engine: str
    engine_active: str | None
    engine_available: bool
    engine_note: str | None
    egress_mode: str | None
    egress_note: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "headless": self.headless,
            "profile_dir": self.profile_dir,
            "kdspy_loaded": self.kdspy_loaded,
            "open_pages": self.open_pages,
            "last_launched_at": self.last_launched_at,
            "last_activity_at": self.last_activity_at,
            "launch_error": self.launch_error,
            "crash_count": self.crash_count,
            "engine": self.engine,
            "engine_active": self.engine_active,
            "engine_available": self.engine_available,
            "engine_note": self.engine_note,
            "egress_mode": self.egress_mode,
            "egress_note": self.egress_note,
        }


class BrowserManager:
    """Owns the single persistent Camoufox context."""

    def __init__(
        self,
        config: Config,
        kdspy: KDSpyManager,
        evidence: BrowserEvidenceStore,
        relay: Any = None,
    ) -> None:
        self.config = config
        self.kdspy = kdspy
        self.evidence = evidence
        self.relay = relay
        self.profile_dir = config.browser_profiles_dir / "kdspy"
        self._playwright: Any = None
        self._context: Any = None
        # Camoufox engine handle (the AsyncCamoufox context manager) — it owns
        # the Playwright driver and is torn down through __aexit__.
        self._camoufox: Any = None
        # Always "camoufox": the engine is fixed. Kept so status consumers can
        # still read engine_active uniformly.
        self._engine_kind: str | None = None
        self._engine_note: str | None = None
        self._lock = asyncio.Lock()
        self._last_activity: float | None = None
        self._last_launched: float | None = None
        self._launch_error: str | None = None
        # Egress actually in effect for the current context: "relay" | "proxy"
        # | "direct" | None. A launch-time health check demotes a dead
        # BROWSER_PROXY to direct egress (policy permitting) instead of
        # letting every navigation die with NS_ERROR_PROXY_CONNECTION_REFUSED.
        self._egress_mode: str | None = None
        self._egress_note: str | None = None
        # Consecutive browser-level proxy refusals with no successful
        # navigation between them. When the browser itself keeps getting
        # refused while the probe still passes, the probe has lost its
        # predictive power and egress is force-demoted to direct (see
        # _escalate_persistent_refusal) — the browser is the source of truth.
        self._proxy_refusal_streak = 0
        self._proxy_refusal_last = 0.0
        # When mid-session recovery decides the proxy is out (even though a
        # flapping probe can still pass), the NEXT launch must not probe it
        # back in: this reason is honored once by launch()'s health gate.
        self._egress_force_direct_reason: str | None = None
        self._crash_count = 0
        self._kdspy_extension_id: str | None = None
        self._idle_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ state
    @property
    def is_running(self) -> bool:
        ctx = self._context
        if ctx is None:
            return False
        # Persistent contexts expose .browser(); a closed context or a closed
        # parent browser means we must relaunch. Avoids relying on private attrs.
        try:
            browser = ctx.browser
            if callable(browser):
                browser = browser()
            if browser is not None and getattr(browser, "is_connected", True) is False:
                return False
        except Exception:
            return False
        return True

    def status(self) -> BrowserStatus:
        return BrowserStatus(
            running=self.is_running,
            headless=self.config.browser_headless,
            profile_dir=str(self.profile_dir),
            kdspy_loaded=self._kdspy_extension_id is not None,
            open_pages=len(self._context.pages) if self.is_running and self._context else 0,
            last_launched_at=_iso_from(self._last_launched),
            last_activity_at=_iso_from(self._last_activity),
            launch_error=self._launch_error,
            crash_count=self._crash_count,
            engine=self.config.browser_engine,
            engine_active=self._engine_kind,
            engine_available=camoufox_engine.CAMOUFOX_AVAILABLE,
            engine_note=self._engine_note,
            egress_mode=self._egress_mode,
            egress_note=self._egress_note,
        )

    # --------------------------------------------------------------- lifecycle
    async def launch(self, *, force: bool = False) -> BrowserStatus:
        """Launch the persistent Camoufox browser if not already running."""
        if not PLAYWRIGHT_AVAILABLE:
            raise BrowserManagerError(
                "playwright is not installed on the server. "
                "Install with: pip install playwright"
            )
        async with self._lock:
            if self.is_running and not force:
                return self.status()
            if self._context is not None:
                await self._teardown_context()
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            # A Firefox profile lock left by a crashed browser ("Camoufox is
            # already running, but is not responding") bricks every later
            # launch until the files are deleted by hand. Clear stale locks
            # before the engine touches the profile.
            _cleanup_stale_profile_lock(self.profile_dir)
            # Egress source, in order of preference: the owner-device relay
            # shim (phone IP mode) → owner-configured external proxy →
            # direct. Both engines share this selection.
            relay = getattr(self, "relay", None)
            relay_addr = (
                f"127.0.0.1:{relay.shim.port}"
                if (relay and relay.enabled and relay.shim and relay.shim.port)
                else None
            )
            # Egress health gate: the owner-configured BROWSER_PROXY must be
            # FUNCTIONALLY usable (protocol handshake, not just a TCP connect)
            # BEFORE it is wired into the browser. A dead or misconfigured
            # proxy otherwise poisons the whole context (every page.goto fails
            # with NS_ERROR_PROXY_CONNECTION_REFUSED). Fall back to direct
            # egress when allowed, or refuse to launch with a clear note.
            if not relay_addr and self.config.browser_proxy:
                # A mid-session recovery may already have decided the proxy
                # is out — even though a flapping probe can still pass right
                # now. Honor that verdict for THIS launch instead of
                # re-probing the refused proxy back in (that rewiring was
                # how NS_ERROR_PROXY_CONNECTION_REFUSED kept reaching the
                # owner after a demotion).
                forced_direct, self._egress_force_direct_reason = (
                    self._egress_force_direct_reason,
                    None,
                )
                if forced_direct:
                    self._egress_mode = "direct"
                    self._egress_note = forced_direct
                else:
                    ok, detail = await probe_proxy(self.config.browser_proxy)
                    if not ok:
                        if self.config.relay_allow_direct:
                            self._egress_note = (
                                f"Configured proxy unusable ({detail}); using direct egress"
                            )
                            self._egress_mode = "direct"
                        else:
                            self._launch_error = (
                                f"Configured proxy unusable and direct egress is "
                                f"disabled: {detail}"
                            )
                            raise BrowserManagerError(self._launch_error)
                    else:
                        self._egress_mode = "proxy"
                        self._egress_note = None
            elif relay_addr:
                self._egress_mode = "relay"
                self._egress_note = None
                self._egress_force_direct_reason = None
            else:
                self._egress_mode = "direct"
                self._egress_note = None
                self._egress_force_direct_reason = None
            # Camoufox is the ONLY engine. A missing install is a hard launch
            # failure with a precise fix — never a silent Chromium fallback.
            if not camoufox_engine.CAMOUFOX_AVAILABLE:
                self._engine_note = "camoufox engine requested but the camoufox package is not installed"
                raise BrowserManagerError(
                    "camoufox is not installed. Resurrección runs Camoufox only — "
                    "fix: pip install 'camoufox[geoip]' && camoufox fetch"
                )
            self._engine_note = None
            try:
                await self._launch_camoufox(relay_addr)
            except Exception as exc:
                self._launch_error = str(exc)
                await self._teardown_context()
                raise BrowserManagerError(f"browser launch failed: {exc}") from exc
            self._launch_error = None
            # A fresh launch gives the proxy a clean slate: its next refusals
            # start a new streak (the escalation ladder gets two chances).
            self._proxy_refusal_streak = 0
            self._proxy_refusal_last = 0.0
            self._last_launched = time.time()
            self._touch()
            self._validate_kdspy_on_camoufox()
            self._arm_idle_shutdown()
            return self.status()

    async def _launch_camoufox(self, relay_addr: str | None) -> None:
        """Launch the persistent Camoufox (anti-detect Firefox) context.

        Camoufox builds the full launch option set (stable profile
        fingerprint, Firefox privacy prefs, egress proxy) in
        app/camoufox_engine.py; this only wires its lifecycle into the
        manager's own. The AsyncCamoufox context manager owns the Playwright
        driver, so teardown goes through its __aexit__ (see below).
        """
        kwargs = camoufox_engine.launch_kwargs(
            self.config,
            relay_addr=relay_addr,
            addons=self.kdspy.firefox_addon_args(),
            egress_mode=self._egress_mode,
        )
        self._camoufox = camoufox_engine.AsyncCamoufox(**kwargs)
        self._context = await self._camoufox.__aenter__()
        self._context.on("crash", self._on_context_crash)
        # DNT/GPC headers + page-side privacy signals are plain Playwright
        # context APIs. No JS stealth patches: Camoufox spoofs natively at
        # the C++ layer and JS patches would only ADD inconsistency.
        privacy.apply_context_privacy(self._context)
        self._engine_kind = "camoufox"

    def _validate_kdspy_on_camoufox(self) -> None:
        """KDSpy state after a camoufox launch.

        The KDSpy Firefox add-on (XPI) loads natively on this engine, so an
        installed add-on is validated and recorded.
        """
        if self.kdspy.firefox_addon_is_configured():
            self.kdspy.firefox_addon_state()
            self.kdspy.record_firefox_runtime_validated(
                str(self.kdspy.firefox_addon_path)
            )
            self._kdspy_extension_id = "camoufox-addon"

    async def _teardown_context(self) -> None:
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
        if self._camoufox is not None:
            # The Camoufox handle owns the Playwright driver when active.
            try:
                await self._camoufox.__aexit__(None, None, None)
            except Exception:
                pass
            self._camoufox = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        self._kdspy_extension_id = None

    async def shutdown(self) -> None:
        """Explicit stop (app shutdown or owner request)."""
        async with self._lock:
            await self._teardown_context()

    def _on_context_crash(self, *_: Any) -> None:
        self._crash_count += 1
        self._context = None

    # ------------------------------------------------------------------ pages
    async def new_page(self, *, interactive: bool = False) -> Any:
        """Open a tracked page in the shared context (bounded pool).

        `interactive=True` marks the page as owner-controlled (sign-in/setup);
        protected pages are never chosen as eviction victims when the research
        page pool needs space. Interactive pages open at a phone-class viewport
        so the owner's taps map 1:1 and every control stays full-size.
        """
        ctx = await self._ensure_context()
        # Respect the 2-core budget: keep at most browser_max_open_pages live,
        # but never evict a page the owner is actively using.
        def _evictable() -> list[Any]:
            return [p for p in ctx.pages if not getattr(p, "_resurreccion_protected", False)]
        while len(_evictable()) >= self.config.browser_max_open_pages:
            victim = _evictable()[0]
            try:
                await victim.close()
            except Exception:
                pass
            else:
                break
            # If closing silently failed and pages didn't shrink, avoid a spin.
            if len(_evictable()) >= self.config.browser_max_open_pages:
                break
        page = await ctx.new_page()
        if interactive:
            page._resurreccion_protected = True
            # Owner-driven pages emulate a REAL PHONE (viewport, touch, mobile
            # UA at every layer) — the shared context stays desktop for
            # research pages. This is what makes the whole screen accurately
            # responsive: the owner taps a 1:1 page where every control is
            # full-size and sites serve their touch layouts.
            try:
                # Playwright-managed viewport survives cross-document
                # navigations; set it first so layout is always phone-class.
                await page.set_viewport_size({
                    "width": self.config.browser_interactive_viewport_w,
                    "height": self.config.browser_interactive_viewport_h,
                })
                await camoufox_engine.apply_interactive_emulation(self.config, page)
            except Exception:
                pass  # page may already be closing; desktop viewport is a safe fallback
        page.set_default_timeout(self.config.browser_default_timeout_seconds * 1000)
        self._touch()
        return page

    async def apply_mobile_emulation(self, page: Any) -> None:
        """Public wrapper: make one existing page emulate a phone.

        Used for popup tabs the site opens after the session started
        (kdspy.com's Login form opens in a new tab) so EVERY tab the owner
        sees stays 1:1 tappable.
        """
        try:
            await page.set_viewport_size({
                "width": self.config.browser_interactive_viewport_w,
                "height": self.config.browser_interactive_viewport_h,
            })
        except Exception:
            pass
        # Rewrites UA headers AND the JS identity for this popup tab.
        await camoufox_engine.apply_interactive_emulation(self.config, page)

    async def open_marketplace(self, marketplace: Marketplace, path: str = "/") -> Any:
        """Navigate a fresh page to a marketplace home/search path."""
        if not path.startswith("/"):
            raise BrowserManagerError("marketplace path must start with '/'")
        url = f"{marketplace.base_url}{path}"
        page = await self.new_page()
        try:
            await self._goto_resilient(page, url)
        except BrowserManagerError:
            # Never leave a half-navigated page behind.
            await self.close_page(page)
            raise
        self._touch()
        return page

    async def navigate(self, page: Any, url: str) -> Any:
        """Navigate a page; returns the page the navigation actually used.

        Normally the same ``page`` given. After an egress recovery the old
        context is gone, so the retry runs on a FRESH page (retrying on the
        old one would hit a torn-down target). Callers that keep using a
        page across navigations should adopt the return value; callers that
        ignore it behave as before.
        """
        try:
            return await self._goto_resilient(page, url)
        except PlaywrightError as exc:  # pragma: no cover - defensive
            raise BrowserManagerError(f"navigation failed: {exc}") from exc

    async def _goto_resilient(self, page: Any, url: str) -> Any:
        """page.goto wrapped in the full egress-recovery ladder.

        One navigation may attempt up to _MAX_NAV_ATTEMPTS gotos, each gated
        by a recovery decision, so the owner never sees
        NS_ERROR_PROXY_CONNECTION_REFUSED unless EVERY recovery path failed:

        1. dead/unusable proxy (probe fails) → relaunch on direct egress,
           retry on a fresh page (_recover_dead_proxy),
        2. refused-but-alive proxy (probe passes; a rate-limited CONNECT
           bridge answering 429/RST to a burst) → one spaced retry on the
           same page (_retry_transient_proxy_failure),
        3. the browser keeps getting refused while the probe keeps passing
           → the probe has lost its predictive power: force-demote to
           direct egress (policy permitting), relaunch, retry on a fresh
           page (_escalate_persistent_refusal). THE browser is the source
           of truth, not the probe.
        """
        target = page
        attempts = 0
        transient_used = False  # the spaced retry fires at most ONCE per navigation
        while True:
            attempts += 1
            try:
                await target.goto(
                    url,
                    timeout=self.config.browser_navigation_timeout_seconds * 1000,
                    wait_until="domcontentloaded",
                )
            except PlaywrightTimeoutError as exc:
                raise BrowserManagerError(f"navigation timeout to {scrub_url(url)}") from exc
            except PlaywrightError as exc:
                if attempts >= _MAX_NAV_ATTEMPTS:
                    # Defensive bound: the ladder below can demand at most
                    # three retries; this guarantees termination even if a
                    # future recovery path ever miscounts.
                    self._proxy_refusal_streak = 0
                    raise BrowserManagerError(f"navigation failed: {exc}") from exc
                if not is_proxy_nav_error(str(exc)):
                    self._proxy_refusal_streak = 0
                    raise BrowserManagerError(f"navigation failed: {exc}") from exc
                self._note_refusal()
                # Escalation ladder, most-accurate-diagnosis first. A True
                # return means the context/egress changed and a retry is
                # worthwhile (on a FRESH page when the context was rebuilt).
                if await self._recover_dead_proxy(exc):
                    target = await self._page_after_recovery(page)
                    continue
                if not transient_used and await self._retry_transient_proxy_failure(exc):
                    transient_used = True
                    continue  # same page: the context never died
                if await self._escalate_persistent_refusal(exc):
                    target = await self._page_after_recovery(page)
                    continue
                raise BrowserManagerError(f"navigation failed: {exc}") from exc
            # Success: the proxy is (currently) usable end-to-end.
            self._proxy_refusal_streak = 0
            self._touch()
            return target
            # (attempts bound guaranteed by the ladder: each recovery fires
            # at most once per navigation; see _MAX_NAV_ATTEMPTS.)

    async def _page_after_recovery(self, old_page: Any) -> Any:
        """Fresh page from the recovered context (the old one is torn down)."""
        try:
            await old_page.close()
        except Exception:
            pass
        return await self.new_page()

    def _note_refusal(self) -> None:
        """Record a browser-level proxy refusal for the escalation ladder."""
        now = time.time()
        if now - self._proxy_refusal_last > _PROXY_REFUSAL_STREAK_WINDOW:
            self._proxy_refusal_streak = 0
        self._proxy_refusal_streak += 1
        self._proxy_refusal_last = now

    async def _escalate_persistent_refusal(self, exc: Exception) -> bool:
        """True when repeated refusals (probe still passing) must demote.

        The incident this exists for: a rate-limited CONNECT bridge answers
        every functional probe with a clean handshake yet refuses every
        browser tunnel for days. The browser's own evidence — consecutive
        NS_ERROR_PROXY_CONNECTION_REFUSED with no successful navigation in
        between — outranks the probe. After _PROXY_REFUSAL_ESCALATE_AFTER
        consecutive refusals, egress is force-demoted to direct (policy
        permitting) and the context relaunched WITHOUT the proxy; the next
        launch's health gate honors the demotion instead of probing the
        refused proxy back in.
        """
        if self._proxy_refusal_streak < _PROXY_REFUSAL_ESCALATE_AFTER:
            return False
        if self._egress_mode != "proxy" or not self.config.browser_proxy:
            return False
        if self.config.relay_allow_direct is False:
            return False  # owner policy: never fall back to direct
        reason = (
            "Browser kept getting refused by the configured proxy "
            f"({self._proxy_refusal_streak} consecutive refusals) even though "
            "the proxy answers health probes; using direct egress"
        )
        # The next launch's gate must NOT probe this proxy back in: the
        # browser just proved the probe wrong.
        self._egress_force_direct_reason = reason
        self._egress_mode = "direct"
        try:
            await self.launch(force=True)
        except BrowserManagerError:
            self._egress_force_direct_reason = None
            return False
        self._egress_note = reason
        return True

    async def _recover_dead_proxy(self, exc: Exception) -> bool:
        """Mid-flight egress recovery when the configured proxy dies.

        A proxy that was healthy at launch (passing the functional health
        gate) can go away later — provider outage, laptop sleep, network
        change. The browser then fails EVERY navigation with
        NS_ERROR_PROXY_* / net::ERR_PROXY_*. When that signature is seen and
        the proxy is genuinely unusable, the context is relaunched without
        it (direct egress, policy permitting) and the caller retries the
        navigation.
        Returns True when recovery happened and a retry is worthwhile.
        """
        if not is_proxy_nav_error(str(exc)):
            return False
        if self._egress_mode != "proxy" or not self.config.browser_proxy:
            return False  # not on the external proxy; nothing to recover
        if self.config.relay_allow_direct is False:
            return False  # owner policy: never fall back to direct
        ok, detail = await probe_proxy(self.config.browser_proxy)
        if ok:
            return False  # transient blip; plain retry will handle it
        reason = (
            f"Configured proxy went away mid-session ({detail}); using direct egress"
        )
        # A flapping proxy can pass the launch-gate probe moments later;
        # force the relaunch to honor the death verdict instead of
        # re-probing the refused proxy back in.
        self._egress_force_direct_reason = reason
        self._egress_mode = "direct"
        try:
            await self.launch(force=True)
        except BrowserManagerError:
            self._egress_force_direct_reason = None
            return False
        self._egress_note = reason
        return True

    async def _retry_transient_proxy_failure(self, exc: Exception) -> bool:
        """True when a proxy-refused navigation deserves one spaced retry.

        A rate-limited CONNECT bridge answers a burst of parallel tunnels
        with HTTP 429 (or an RST); Firefox reports BOTH as
        NS_ERROR_PROXY_CONNECTION_REFUSED even though the proxy is alive —
        the main-document tunnel just lost the race. The launch prefs cap
        concurrency so bursts don't happen; this retry is the belt-and-braces
        for the residual case (background traffic racing a navigation).
        Only fires when the proxy still passes the functional probe (a truly
        dead proxy belongs to _recover_dead_proxy, which relaunches), and at
        most once per navigation.
        """
        if not is_proxy_nav_error(str(exc)):
            return False
        if self._egress_mode != "proxy" or not self.config.browser_proxy:
            return False
        ok, _detail = await probe_proxy(self.config.browser_proxy)
        if not ok:
            return False  # genuinely unusable: let _recover_dead_proxy handle it
        await asyncio.sleep(1.0)  # let the proxy's rate window drain
        return True

    async def close_page(self, page: Any) -> None:
        try:
            await page.close()
        except Exception:
            pass
        self._touch()

    async def pages(self) -> list[Any]:
        ctx = await self._ensure_context()
        return list(ctx.pages)

    # ------------------------------------------------------------- screenshots
    async def screenshot(self, page: Any) -> bytes:
        try:
            data = await page.screenshot(type="png", full_page=False)
        except Exception as exc:
            raise BrowserManagerError(f"screenshot failed: {exc}") from exc
        self._touch()
        return data

    async def live_screenshot(self, session_id: str) -> bytes | None:
        """Best-effort screenshot of the most recent page for the live view.

        Never launches a browser just for watching: returns None when no
        browser/page exists. Never raises — the live view is optional.
        """
        try:
            if not self.is_running or not self._context:
                return None
            pages = [p for p in self._context.pages if not getattr(p, "_resurreccion_protected", False)]
            if not pages:
                return None
            data = await pages[-1].screenshot(type="png", full_page=False)
            self._touch()
            return data
        except Exception:
            return None

    # -------------------------------------------------------------- idle guard
    def _arm_idle_shutdown(self) -> None:
        if self._idle_task is not None:
            return
        self._idle_task = asyncio.create_task(self._idle_loop())

    async def _idle_loop(self) -> None:
        """Shut the browser down after N idle seconds to free the 2 cores."""
        while True:
            await asyncio.sleep(30)
            if not self.is_running:
                continue
            last = self._last_activity or 0
            idle_for = time.time() - last
            if idle_for >= self.config.browser_idle_shutdown_seconds:
                async with self._lock:
                    # Double-check under the lock.
                    last = self._last_activity or 0
                    if time.time() - last >= self.config.browser_idle_shutdown_seconds:
                        await self._teardown_context()

    def _touch(self) -> None:
        self._last_activity = time.time()

    async def _ensure_context(self) -> Any:
        if not self.is_running:
            await self.launch()
        assert self._context is not None
        return self._context


def _iso_from(ts: float | None) -> str | None:
    if ts is None:
        return None
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _cleanup_stale_profile_lock(profile_dir: Any) -> str | None:
    """Remove a Firefox profile lock held by a dead process, if any.

    Firefox guards a profile with a `lock` symlink (target ``host:+pid``)
    plus a `.parentlock` file. A browser crash/kill leaves them behind, and
    every later launch of the SAME profile fails with "Camoufox is already
    running, but is not responding" until someone deletes the files by
    hand. Before launching, parse the pid from the symlink target and only
    clear the lock when no live process holds it — a genuinely running
    browser keeps its lock. Best-effort: never raises (the launch gate must
    not die on a bookkeeping file), so a non-path profile_dir (tests) or
    odd filesystem states simply skip the cleanup.
    Returns the removed lock description for logging, or None.
    """
    try:
        lock_path = profile_dir / "lock"
        parentlock = profile_dir / ".parentlock"
        if not lock_path.is_symlink() and not parentlock.exists():
            return None
        pid = _pid_from_firefox_lock(lock_path)
        if pid is not None and os.path.exists(f"/proc/{pid}"):
            return None  # a live process owns this profile — leave it alone
        removed = f"lock -> {os.readlink(lock_path)}" if lock_path.is_symlink() else ".parentlock"
        for stale in (lock_path, parentlock):
            if stale.is_symlink() or stale.exists():
                stale.unlink()
        return removed
    except Exception:
        return None  # best effort; launch may still fail with its own error


def _pid_from_firefox_lock(lock_path: Any) -> int | None:
    """Extract the pid from a Firefox `lock` symlink target.

    Targets look like ``<inet>:+1181`` (host:port form where the port field
    carries ``+<pid>``). Unparseable targets return None, which the caller
    treats as "no live owner provable" → the lock is cleared (a lock we
    cannot attribute would otherwise brick the launch).
    """
    try:
        target = os.readlink(lock_path)
    except Exception:
        return None
    tail = target.rsplit(":", 1)[-1].strip()
    if tail.startswith("+") and tail[1:].isdigit():
        return int(tail[1:])
    return None
