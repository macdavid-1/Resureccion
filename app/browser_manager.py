"""Playwright persistent browser manager.

Operates ONE real, persistent Chromium via Playwright for the whole app (the
2-core/16GB environment cannot afford a browser per action). The browser:

- uses a persistent user-data dir (`browser_profiles/kdspy`) so cookies,
  localStorage, and extension state survive restarts — this is how the
  owner's Amazon/KDSpy authentication is reused across research runs,
- loads the KDSpy Pro extension (persistent context is required for
  extensions; headless works via the bundled chromium channel),
- is launched lazily on first use and shut down after an idle period,
- tracks crashes and restarts itself cleanly.

This module is the ONLY place that touches playwright APIs. Credentials,
cookies, and storage state never leave here except into the profile dir.

The playwright import is deferred so the app (and tests) run on machines
without playwright installed — every public API raises `BrowserManagerError`
with a clear message in that case, and the rest of Resurrección works.
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.browser_store import BrowserEvidenceStore
from app.config import Config
from app.kdspy import KDSpyManager
from app.marketplace import Marketplace, get_marketplace
from app.redact import scrub_url
from app import stealth
from app import privacy

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
        }


class BrowserManager:
    """Owns the single persistent Chromium context."""

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
        self._lock = asyncio.Lock()
        self._last_activity: float | None = None
        self._last_launched: float | None = None
        self._launch_error: str | None = None
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
        )

    # --------------------------------------------------------------- lifecycle
    async def launch(self, *, force: bool = False) -> BrowserStatus:
        """Launch the persistent Chromium if not already running."""
        if not PLAYWRIGHT_AVAILABLE:
            raise BrowserManagerError(
                "playwright is not installed on the server. "
                "Install with: pip install playwright && playwright install chromium"
            )
        async with self._lock:
            if self.is_running and not force:
                return self.status()
            if self._context is not None:
                await self._teardown_context()
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            args = [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ]
            # Egress source, in order of preference: the owner-device relay
            # shim (phone IP mode) → owner-configured external proxy →
            # direct. When the relay shim is running it is the single egress
            # point: it serves the device first and falls back itself.
            relay = getattr(self, "relay", None)
            relay_addr = f"127.0.0.1:{relay.shim.port}" if (relay and relay.enabled and relay.shim and relay.shim.port) else None
            if relay_addr:
                args.append(f"--proxy-server=http={relay_addr};https={relay_addr}")
            elif self.config.browser_proxy:
                args.append(f"--proxy-server={self.config.browser_proxy}")
            # Privacy hardening: tracker/ad hosts are refused at the egress
            # shim (app/privacy.py); WebRTC can never bypass the proxy over
            # UDP; Chromium background services stay silent; DNT/GPC are
            # declared on every request. Apply whenever the browser launches
            # so the protections do not depend on relay mode.
            args.extend(privacy.relay_launch_args(args))
            # Containers run as root without user namespaces; Chromium needs
            # --no-sandbox there (auto-detected, BROWSER_NO_SANDBOX overrides).
            if self.config.browser_no_sandbox:
                args.append("--no-sandbox")
            args.extend(self.config.browser_extra_args)
            args.extend(self.kdspy.chromium_args())
            launch_kwargs: dict[str, Any] = {
                "user_data_dir": str(self.profile_dir),
                "headless": self.config.browser_headless,
                "args": args,
                "locale": self.config.browser_locale,
                "timezone_id": self.config.browser_timezone,
                "viewport": {"width": 1440, "height": 900},
                "accept_downloads": True,
            }
            # `channel` selects branded Chrome/Edge/MsEdge installs; the bundled
            # Chromium must NOT receive a channel kwarg (it errors or resolves wrong).
            if self.config.browser_channel and self.config.browser_channel.lower() not in ("chromium", ""):
                launch_kwargs["channel"] = self.config.browser_channel
            elif self.kdspy.chromium_args() and self.config.browser_headless:
                # The old headless shell cannot run extensions at all; the
                # "chromium" channel opts into new headless mode, which can.
                launch_kwargs["channel"] = "chromium"
            if self.config.browser_user_agent:
                launch_kwargs["user_agent"] = self.config.browser_user_agent
            try:
                self._playwright = await async_playwright().start()
                self._context = await self._playwright.chromium.launch_persistent_context(
                    **launch_kwargs
                )
                self._context.on("crash", self._on_context_crash)
                # Anti-automation-detection patches (navigator.webdriver etc.)
                # for EVERY page in this context, installed once at the context
                # level so sign-in pages AND research pages both benefit.
                stealth.apply(self._context)
                # Declare the owner's privacy preference on every request and
                # page (DNT/Sec-GPC headers, navigator.globalPrivacyControl).
                privacy.apply_context_privacy(self._context)
            except Exception as exc:
                self._launch_error = str(exc)
                await self._teardown_context()
                raise BrowserManagerError(f"Chromium launch failed: {exc}") from exc
            self._launch_error = None
            self._last_launched = time.time()
            self._touch()
            # Verify KDSpy service worker presence (best-effort, MV3).
            await self._verify_kdspy_runtime()
            self._arm_idle_shutdown()
            return self.status()

    async def _verify_kdspy_runtime(self) -> None:
        """Detect the KDSpy service worker / extension id once pages settle."""
        ctx = self._context
        if ctx is None or not self.kdspy.is_configured():
            return
        try:
            workers = list(getattr(ctx, "service_workers", []) or [])
            if not workers:
                try:
                    worker = await asyncio.wait_for(
                        ctx.wait_for_event("serviceworker"), timeout=8
                    )
                    workers = [worker]
                except (asyncio.TimeoutError, Exception):
                    workers = []
            if not workers and ctx.pages:
                # MV3 service workers may only spin up once a page exists;
                # give the context's initial page a moment, then re-check.
                await asyncio.sleep(2.5)
                workers = list(getattr(ctx, "service_workers", []) or [])
            if workers:
                url = workers[0].url
                ext_id = url.split("/")[2] if url.count("/") >= 2 else ""
                self._kdspy_extension_id = ext_id
                self.kdspy.record_runtime_validated(ext_id, scrub_url(url))
            else:
                # Headless-MV3 quirks may delay SW startup; not fatal here.
                self.kdspy.record_runtime_failure(
                    "service worker not observed within 10s of launch"
                )
        except Exception as exc:  # never break launch on verification
            self.kdspy.record_runtime_failure(f"verification error: {exc}")

    async def _teardown_context(self) -> None:
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
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
            # UA, meta-viewport handling) — the shared context stays desktop
            # for research pages. This is what makes the whole screen
            # accurately responsive: the owner taps a 1:1 page where every
            # control is full-size and sites serve their touch layouts.
            try:
                # Playwright-managed viewport survives cross-document
                # navigations; set it first so layout is always phone-class.
                await page.set_viewport_size({
                    "width": self.config.browser_interactive_viewport_w,
                    "height": self.config.browser_interactive_viewport_h,
                })
                await self._emulate_mobile(page)
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
        await self._emulate_mobile(page)

    async def _emulate_mobile(self, page: Any) -> None:
        """Turn one page into a phone: mobile metrics, touch, mobile UA.

        Uses a per-page CDP session so the shared research context is
        untouched. Runs before first navigation, so every site (kdspy.com,
        Amazon, WordPress login) sees a genuine mobile browser.
        """
        w = self.config.browser_interactive_viewport_w
        h = self.config.browser_interactive_viewport_h
        cdp = await page.context.new_cdp_session(page)
        if self.config.browser_interactive_user_agent:
            await cdp.send(
                "Emulation.setUserAgentOverride",
                {"userAgent": self.config.browser_interactive_user_agent},
            )
        await cdp.send(
            "Emulation.setTouchEmulationEnabled",
            {"enabled": True, "maxTouchPoints": 5},
        )
        await cdp.send(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": w,
                "height": h,
                "deviceScaleFactor": 1,
                "mobile": True,
            },
        )

    async def open_marketplace(self, marketplace: Marketplace, path: str = "/") -> Any:
        """Navigate a fresh page to a marketplace home/search path."""
        if not path.startswith("/"):
            raise BrowserManagerError("marketplace path must start with '/'")
        page = await self.new_page()
        url = f"{marketplace.base_url}{path}"
        try:
            await page.goto(
                url,
                timeout=self.config.browser_navigation_timeout_seconds * 1000,
                wait_until="domcontentloaded",
            )
        except PlaywrightTimeoutError as exc:
            raise BrowserManagerError(f"navigation timeout to {scrub_url(url)}") from exc
        except PlaywrightError as exc:
            raise BrowserManagerError(f"navigation failed: {exc}") from exc
        self._touch()
        return page

    async def navigate(self, page: Any, url: str) -> None:
        try:
            await page.goto(
                url,
                timeout=self.config.browser_navigation_timeout_seconds * 1000,
                wait_until="domcontentloaded",
            )
        except PlaywrightTimeoutError as exc:
            raise BrowserManagerError(f"navigation timeout to {scrub_url(url)}") from exc
        except PlaywrightError as exc:
            raise BrowserManagerError(f"navigation failed: {exc}") from exc
        self._touch()

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
