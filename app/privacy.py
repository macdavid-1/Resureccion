"""Privacy hardening for the research browser's egress.

When the browser exits through the owner's phone relay (or any proxy), the
owner's own network identity is on every request. This module keeps that
exposure minimal and deliberate:

1. **Tracker blocklist** — ad/analytics/fingerprinting hosts are refused by
   the relay proxy shim BEFORE any request reaches the phone's network, so
   blocked parties never learn the exit IP at all. The list is explicit,
   curated from the widely-maintained core of public ad/tracker blocklists
   (EasyList/StevenBlack core domains) and covers the trackers that ship on
   retail/marketplace pages. Research targets themselves (marketplaces,
   kdspy.com) are never on the list.

2. **WebRTC lockdown** — WebRTC can bypass HTTP proxies entirely (UDP
   transport), which would leak the SERVER's IP (and, over the relay, would
   try to punch through the phone). Research pages have no legitimate use
   for peer connections, so non-proxied UDP is disabled at launch.

3. **Telemetry dial-home suppression** — Chromium's own background
   services (safe-browsing updates, optimization-guide fetches, component
   updates) would otherwise contact Google directly. They are disabled:
   nothing about the owner's browsing leaves the box except the research
   requests themselves.

4. **DNT / Global Privacy Control** — the owner's chosen preference is
   declared on every request and page (`DNT: 1`, `Sec-GPC: 1`,
   `navigator.globalPrivacyControl`), the standard legal signals sites are
   required to honor in several jurisdictions.

Everything here is honest hardening: it changes WHERE requests go and
declares the owner's stated privacy preferences; it never alters page
content, research observations, or evidence.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Tracker/ad blocklist (suffix-matched: "doubleclick.net" blocks all its
# subdomains). Provenance: intersection of the core EasyList and StevenBlack
# hosts domains most commonly embedded on retail/marketplace pages. Kept as
# an explicit, reviewable list rather than a fetched third-party file so the
# deployed behavior is deterministic and auditable.
# ---------------------------------------------------------------------------
BLOCKED_DOMAIN_SUFFIXES: frozenset[str] = frozenset(
    {
        # Google ads / analytics
        "doubleclick.net",
        "googlesyndication.com",
        "google-analytics.com",
        "googletagmanager.com",
        "googletagservices.com",
        "googleadservices.com",
        "adservice.google.com",
        "pagead2.googlesyndication.com",
        "app-measurement.com",
        "firebaseinstallations.googleapis.com",
        # Large ad exchanges / networks
        "adnxs.com",
        "adsrvr.org",
        "adsystem.amazon.com" ,
        "amazon-adsystem.com",
        "criteo.com",
        "criteo.net",
        "casalemedia.com",
        "openx.net",
        "pubmatic.com",
        "rubiconproject.com",
        "smartadserver.com",
        "adform.net",
        "adroll.com",
        "bidswitch.net",
        "bidr.io",
        "bluekai.com",
        "branch.io",
        "demdex.net",
        "everesttech.net",
        "mathtag.com",
        "moatads.com",
        "ogury.com",
        "onesignal.com",
        "outbrain.com",
        "rlcdn.com",
        "sharethrough.com",
        "spotxchange.com",
        "taboola.com",
        "teads.tv",
        "yieldmo.com",
        "zedo.com",
        # Measurement / analytics
        "scorecardresearch.com",
        "quantserve.com",
        "quantcount.com",
        "chartbeat.com",
        "chartbeat.net",
        "mixpanel.com",
        "segment.com",
        "segment.io",
        "amplitude.com",
        "heapanalytics.com",
        "kissmetrics.com",
        "mouseflow.com",
        "fullstory.com",
        "hotjar.com",
        "hotjar.io",
        "clarity.ms",
        "statcounter.com",
        "newrelic.com",
        "nr-data.net",
        "bugsnag.com",
        "optimizely.com",
        "crazyegg.com",
        "luckyorange.com",
        "yandex.ru",
        "mc.yandex.ru",
        "cnzz.com",
        "hm.baidu.com",
        # Social tracking pixels (social content itself is not blocked)
        "connect.facebook.net",
        "analytics.tiktok.com",
        "ads-twitter.com",
        "static.ads-twitter.com",
        "snap.licdn.com",
        "bat.bing.com",
        "clarity.microsoft.com",
        # Fingerprinting-adjacent
        "fingerprintjs.com",
        "fpjs.io",
        "iovation.com",
        "perimeterx.net",
        "px-cdn.net",
        # STUN/TURN — WebRTC discovery must not leak through UDP either
        "stun.l.google.com",
        "stun1.l.google.com",
        "stun2.l.google.com",
        "stun3.l.google.com",
        "stun4.l.google.com",
        "stun.cloudflare.com",
        "global.stun.twilio.com",
    }
)

# Hosts the system itself must reach even when a suffix above would match.
ALLOWED_DOMAIN_EXCEPTIONS: frozenset[str] = frozenset(
    {
        # Amazon marketplaces and KDSpy are research targets; nothing on the
        # blocklist is a suffix of these, but the guard documents intent and
        # protects against future list edits.
        "amazon.com",
        "amazon.co.uk",
        "amazon.de",
        "kdspy.com",
        "publishingaltitude.com",
    }
)


def is_blocked_host(host: str) -> bool:
    """True when host (or its parent domain) is on the tracker blocklist."""
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return False
    # Strip a port if a caller passed host:port.
    if ":" in h and not h.endswith("]"):
        h = h.rsplit(":", 1)[0]
    if h in ALLOWED_DOMAIN_EXCEPTIONS:
        return False
    if h in BLOCKED_DOMAIN_SUFFIXES:
        return True
    return any(h.endswith("." + suffix) for suffix in BLOCKED_DOMAIN_SUFFIXES)


def blocked_check(host: str) -> str | None:
    """Human-readable reason string when host is blocked, else None."""
    return f"blocked by privacy filter ({host})" if is_blocked_host(host) else None


# ---------------------------------------------------------------------------
# Chromium launch flags: privacy hardening that does not affect rendering or
# research behavior.
# ---------------------------------------------------------------------------
PRIVACY_CHROMIUM_ARGS: tuple[str, ...] = (
    # WebRTC must never bypass the proxy via UDP (IP-leak vector). Valid
    # values: default | default_public_interface_only |
    # default_public_and_private_interfaces | disable_non_proxied_udp.
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
    # Don't leak local IPs via mDNS candidates either.
    "--enable-features=WebRtcHideLocalIpsWithMdns",
    # Chromium background phone-home (safe-browsing pings, optimization
    # guide fetches, component updates, domain reliability) — off. Research
    # pages are reached directly; nothing else should dial out.
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-domain-reliability",
    "--disable-client-side-phishing-detection",
    "--disable-sync",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-features=OptimizationGuideModelDownloading,OptimizationHintsFetching,OptimizationTargetPrediction,OptimizationHints",
)

# Per-request privacy headers applied at the context level (every page and
# subresource request, including through the relay).
PRIVACY_EXTRA_HEADERS: dict[str, str] = {
    "DNT": "1",
    "Sec-GPC": "1",
}

# Page-side preference signals, appended to the stealth init script chain.
PRIVACY_INIT_SCRIPT = r"""
(() => {
  try { Object.defineProperty(Navigator.prototype, 'globalPrivacyControl', { get: () => true, configurable: true }); } catch (e) {}
  try { Object.defineProperty(Navigator.prototype, 'doNotTrack', { get: () => '1', configurable: true }); } catch (e) {}
})();
"""


def relay_launch_args(extra_existing: list[str] | None = None) -> list[str]:
    """Launch args to add when the relay/privacy layer is active.

    Deduplicated against existing args so a double application is harmless.
    """
    existing = list(extra_existing or [])
    out: list[str] = []
    for arg in PRIVACY_CHROMIUM_ARGS:
        if arg not in existing:
            out.append(arg)
    return out


def apply_context_privacy(context: Any) -> None:
    """Attach DNT/GPC headers AND page-side preference signals to a context.

    Fire-and-forget on purpose (same scheduling convention as stealth.apply):
    the caller is usually sync-adjacent and a failure must never block the
    launch path.
    """
    import asyncio

    def _sched(coro: Any) -> None:
        if asyncio.iscoroutine(coro):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                coro.close()
                return
            loop.create_task(coro)

    try:
        _sched(context.set_extra_http_headers(dict(PRIVACY_EXTRA_HEADERS)))
    except Exception:
        pass
    try:
        _sched(context.add_init_script(PRIVACY_INIT_SCRIPT))
    except Exception:
        pass
