"""Camoufox engine — a real Firefox with anti-detect spoofing at the C++ layer.

Resurrección's research browser can run on two engines:

- ``chromium`` — Playwright's bundled Chromium. Required for Chrome MV3
  extensions (KDSpy Pro) and supports CDP-based per-page mobile emulation.
- ``camoufox`` — Camoufox (Firefox fork) via ``camoufox.async_api``. Fingerprint
  consistency (canvas/audio/fonts/WebGL/timezone/locale) is enforced natively by
  the browser binary, so the anti-detect surface stops depending on JS patches
  and the whole "why was the tap refused" class of Chromium headless tells goes
  away. Cross-origin CAPTCHA iframes (Turnstile) are clickable thanks to the
  engine's ``disable_coop`` option.

Everything specific to the Camoufox engine lives in this module so
``app/browser_manager.py`` stays engine-agnostic: it asks this module for
launch kwargs and applies them through ``camoufox.async_api.AsyncNewBrowser``.
When camoufox is not installed, the module degrades to ``False`` and callers
fall back to Chromium with a clear status reason.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.config import Config
from app.proxy_spec import parse_proxy_spec, proxy_server_url


def _has_camoufox_install(cache_home: str) -> bool:
    """True when ``cache_home`` holds a camoufox download (any layout)."""
    root = Path(cache_home) / "camoufox"
    # Current layout: <cache>/camoufox/browsers/<repo>/<version>/. Also
    # accept the legacy flat layout (version.json at the root).
    return (root / "browsers").is_dir() or (root / "version.json").is_file()


def _ensure_camoufox_cache_env() -> None:
    """Point the camoufox package at the cache that actually holds the binary.

    ``camoufox.pkgman`` computes its install dir ONCE at import time from
    ``user_cache_dir("camoufox")`` — i.e. from the *process's* HOME /
    XDG_CACHE_HOME. When the account that fetched the browser (``camoufox
    fetch``) differs from the account running the server — dev sandboxes,
    containers that switch users, HF Spaces restarts with a different HOME —
    the package looks in an empty cache and launch fails with "official/stable
    is not installed. Please run `camoufox fetch` to install" even though the
    binary exists on disk.

    Before camoufox is first imported, locate an existing download under any
    user's cache and export the matching XDG_CACHE_HOME so the resolved
    INSTALL_DIR is the one that holds it. A no-op when the current resolution
    already has the install (the deployment case). CAMOUFOX_CACHE pins the
    cache explicitly and always wins.
    """
    if os.environ.get("CAMOUFOX_CACHE"):
        os.environ["XDG_CACHE_HOME"] = os.environ["CAMOUFOX_CACHE"]
        return

    current = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    if _has_camoufox_install(current):
        return
    for candidate in sorted(Path("/home").glob("*/.cache")):
        cand = str(candidate)
        if cand == current:
            continue
        if _has_camoufox_install(cand):
            os.environ["XDG_CACHE_HOME"] = cand
            # Firefox refuses to run as root under a HOME owned by another
            # user; give root a root-owned HOME when we just switched cache.
            if (
                hasattr(os, "geteuid")
                and os.geteuid() == 0
                and os.environ.get("HOME", "") == str(candidate.parent)
            ):
                os.environ["HOME"] = "/root"
                try:
                    Path("/root/.camoufox").mkdir(parents=True, exist_ok=True)
                except OSError:
                    pass
            return


_ensure_camoufox_cache_env()

try:  # pragma: no cover - import guard mirrors browser_manager's convention
    from browserforge.fingerprints import Fingerprint
    from camoufox.async_api import AsyncCamoufox, AsyncNewBrowser
    from camoufox.fingerprints import generate_fingerprint

    CAMOUFOX_AVAILABLE = True
except ImportError:  # pragma: no cover
    CAMOUFOX_AVAILABLE = False
    AsyncCamoufox = None  # type: ignore[assignment,misc]
    AsyncNewBrowser = None  # type: ignore[assignment]
    generate_fingerprint = None  # type: ignore[assignment]
    Fingerprint = None  # type: ignore[assignment,misc]

# Default OS families the profile fingerprint may draw from. The first
# generation picks one and caches it — the cache keeps every later launch
# coherent (same UA/platform/screen forever), which is the whole point of a
# persistent research identity.
DEFAULT_OS_CHOICES = ("windows", "macos")


def firefox_ua(version: str = "130.0", platform: str = "windows") -> str:
    """A current, internally-consistent Firefox UA for the engine family."""
    templates = {
        "windows": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{ver}) Gecko/20100101 Firefox/{ver}"
        ),
        "macos": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:{ver}) "
            "Gecko/20100101 Firefox/{ver}"
        ),
        "linux": (
            "Mozilla/5.0 (X11; Linux x86_64; rv:{ver}) Gecko/20100101 Firefox/{ver}"
        ),
    }
    template = templates.get(platform, templates["windows"])
    return template.format(ver=version)


# --------------------------------------------------------------------------- prefs
# Firefox prefs for the research browser. Same intents as app/privacy.py's
# Chromium flags, expressed as about:config prefs (Chromium-only flags like
# --webrtc-ip-handling-policy do not exist in Firefox; media.peerconnection.*
# covers the same leak vector).
FIREFOX_USER_PREFS: dict[str, Any] = {
    # WebRTC must never bypass the proxy: no peer connections at all on the
    # research browser (research pages have no legitimate use for them).
    "media.peerconnection.enabled": False,
    # Belt-and-braces: ICE must not even consider local addresses.
    "media.peerconnection.ice.default_address_only": True,
    "media.peerconnection.ice.no_host": True,
    # Telemetry / dial-home suppression (privacy.py's intent, Firefox form).
    "toolkit.telemetry.enabled": False,
    "toolkit.telemetry.archive.enabled": False,
    "toolkit.telemetry.unified": False,
    "datareporting.policy.dataSubmissionEnabled": False,
    "datareporting.healthreport.uploadEnabled": False,
    "app.shield.optoutstudies.enabled": False,
    "browser.discovery.enabled": False,
    "browser.newtabpage.activity-stream.feeds.telemetry": False,
    # DNT + GPC: the owner's privacy preference, declared natively.
    "privacy.donottrackheader.enabled": True,
    "privacy.globalprivacycontrol.enabled": True,
    # First-run/prompt noise would otherwise stall an unattended session.
    "browser.shell.checkDefaultBrowser": False,
    "browser.startup.homepage_override.mstone": "ignore",
    "browser.aboutwelcome.enabled": False,
    "permissions.default.geo": 2,
    "permissions.default.desktop-notification": 2,
    # Long-run stability under the 2-core budget.
    "dom.ipc.processCount": 2,
    "browser.tabs.unloadOnLowMemory": True,
}


def interactive_ua(config: Config) -> str:
    """Mobile UA for owner-driven interactive pages, Firefox-shaped.

    The Chromium pipeline used a Chrome-on-Android UA for interactive pages.
    Camoufox is Firefox — a Chrome UA would contradict the engine itself (an
    instant tell). We keep the Android intent (sites serve their touch
    layouts) as a Firefox-mobile UA.
    """
    ua = config.browser_interactive_user_agent
    if "Firefox" in ua:
        return ua
    return "Mozilla/5.0 (Android 13; Mobile; rv:130.0) Gecko/130.0 Firefox/130.0"


def stable_fingerprint_path(config: Config) -> Path:
    """Where this profile's fingerprint JSON lives (persistent bucket)."""
    return config.browser_profiles_dir / "kdspy_fingerprint.json"


def _load_cached_fingerprint(path: Path) -> "Fingerprint | None":
    """Read the cached fingerprint and fully re-type its nested dataclasses.

    ``asdict`` flattens NavigatorFingerprint/ScreenFingerprint/VideoCard into
    plain dicts; a naive ``Fingerprint(**dict)`` would produce an object whose
    ``.navigator`` is a dict (broken for callers and for camoufox's own
    conversion). Reconstruct every nested dataclass explicitly.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if not CAMOUFOX_AVAILABLE:
        return None
    from browserforge.fingerprints import (
        NavigatorFingerprint,
        ScreenFingerprint,
        VideoCard,
    )

    nav, screen, card = data.get("navigator"), data.get("screen"), data.get("videoCard")
    if not isinstance(nav, dict) or not isinstance(screen, dict):
        return None  # corrupt/partial cache → regenerate cleanly
    try:
        navigator = NavigatorFingerprint(**nav)
        fp_screen = ScreenFingerprint(**screen)
        video_card = VideoCard(**card) if isinstance(card, dict) else None
        rest = {k: v for k, v in data.items() if k not in ("navigator", "screen", "videoCard")}
        return Fingerprint(navigator=navigator, screen=fp_screen, videoCard=video_card, **rest)
    except TypeError:
        # Field set drift between library versions → regenerate.
        return None


def _persist_fingerprint(path: Path, fp_dict: dict[str, Any]) -> None:
    """Durable write (temp + replace) so a crash never truncates the cache."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(fp_dict), encoding="utf-8")
    os.replace(tmp, path)


def load_or_generate_fingerprint(config: Config) -> "Fingerprint":
    """Profile-stable identity: generate once, cache as JSON, reuse forever.

    Reusing the same fingerprint across restarts is the point: a research
    profile that changes hardware/fingerprint on every relaunch is itself an
    anomaly signal. The cache lives under DATA_DIR (persistent storage), so
    Space restarts do not rotate the identity either.
    """
    if not CAMOUFOX_AVAILABLE:
        raise RuntimeError("camoufox is not installed")
    path = stable_fingerprint_path(config)
    cached = _load_cached_fingerprint(path)
    if cached is not None:
        return cached
    fp = generate_fingerprint(os=list(DEFAULT_OS_CHOICES))
    _persist_fingerprint(path, asdict(fp))
    return fp


def _proxy_kwargs(config: Config, relay_addr: str | None) -> dict[str, Any]:
    """Egress selection shared with the Chromium path: relay shim → proxy.

    The proxy spec's scheme is preserved (socks5:// for a Cloudflare WARP
    bridge on a VPS, http:// for Webshare-style CONNECT proxies). Firefox
    applies SOCKS5 username/password auth natively.
    """
    if relay_addr:
        return {"proxy": {"server": f"http://{relay_addr}"}}
    if config.browser_proxy:
        server, username, password = parse_proxy_spec(config.browser_proxy)
        if server:
            proxy: dict[str, str] = {"server": proxy_server_url(config.browser_proxy)}
            if username:
                proxy["username"] = username
                proxy["password"] = password or ""
            return {"proxy": proxy}
    return {}


def launch_kwargs(
    config: Config,
    *,
    relay_addr: str | None = None,
    addons: list[str] | None = None,
) -> dict[str, Any]:
    """Playwright Firefox launch options for the Camoufox engine.

    Returns kwargs for ``camoufox.async_api.AsyncNewBrowser`` (which forwards
    to ``firefox.launch_persistent_context``): the stable profile fingerprint,
    Firefox privacy prefs, the egress setup (relay shim → owner proxy →
    direct) that the Chromium path uses, and any Firefox add-ons (e.g. the
    KDSpy Firefox add-on, which the engine loads natively).
    """
    if not CAMOUFOX_AVAILABLE:
        raise RuntimeError("camoufox is not installed")

    kwargs: dict[str, Any] = {
        "persistent_context": True,
        "user_data_dir": str(config.browser_profiles_dir / "kdspy"),
        "headless": config.browser_headless,
        "os": list(DEFAULT_OS_CHOICES),
        # The config layer sets Camoufox properties deliberately; suppress the
        # library's manual-config warnings (they are advisory only).
        "i_know_what_im_doing": True,
        # Cross-origin CAPTCHA iframes (Turnstile/reCAPTCHA) must be
        # interactive: the engine disables Cross-Origin-Opener-Policy so
        # cross-origin iframe elements can receive real clicks.
        "disable_coop": True,
        # WebRTC is disabled at the pref level below; block_webrtc also stops
        # the engine from enumerating host interfaces at launch.
        "block_webrtc": True,
        # Research pages are desktop-class; no_viewport lets the spoofed
        # window dimensions drive layout instead of Playwright's fixed
        # viewport fighting the spoof.
        "no_viewport": True,
        "firefox_user_prefs": dict(FIREFOX_USER_PREFS),
    }
    if config.camoufox_humanize:
        # Humanized cursor movement — helps interactive pages feel real to
        # behavioral checks. Disabled by default: the owner taps by hand.
        kwargs["humanize"] = config.camoufox_humanize
    if config.camoufox_geoip:
        # Derive timezone/locale/geo from the egress IP so the spoof is
        # coherent with the exit network (a US IP with a Paris clock is a
        # classic tell). Requires the geoip extra; guarded by config.
        kwargs["geoip"] = True
    kwargs.update(_proxy_kwargs(config, relay_addr))

    kwargs["fingerprint"] = load_or_generate_fingerprint(config)
    # Firefox add-ons (extracted dirs with a manifest.json): Camoufox loads
    # them natively at launch (camoufox.utils.launch_options → config[
    # "addons"]). Empty/None keeps the default uBlock-Origin flow untouched.
    if addons:
        kwargs["addons"] = [str(a) for a in addons]
    return kwargs


def apply_interactive_emulation(config: Config, page: Any) -> None:
    """Owner-driven pages: Firefox-mobile UA so sites serve touch layouts.

    CDP does not exist on Firefox, so the Chromium per-page CDP path cannot be
    used. The page-level lever that matters for the owner's tap experience is
    the UA (sites serve their touch layouts); the 1:1 viewport itself is
    engine-independent and stays in browser_manager (set_viewport_size).
    """
    ua = interactive_ua(config)
    script = (
        "(() => { try { Object.defineProperty(navigator, 'userAgent', "
        f"{{ get: () => {json.dumps(ua)}, configurable: true }}); }} catch (e) {{}} )();"
    )
    try:
        coro = page.add_init_script(script)
        if asyncio.iscoroutine(coro):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                coro.close()
                return
            loop.create_task(_swallow(coro))
    except Exception:
        # add_init_script failing is non-fatal; the page just keeps the
        # profile-level spoofed UA (still a plausible Firefox UA).
        pass


async def _swallow(coro: Any) -> None:
    try:
        await coro
    except Exception:
        pass
