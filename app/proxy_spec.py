"""Proxy spec parsing shared by the browser launcher and the relay shim.

Accepts the owner-facing forms:
    host:port
    user:pass@host:port
    http://host:port
    http://user:pass@host:port
    socks5://user:pass@host:port        (SOCKS5, e.g. a Cloudflare WARP bridge)
    socks5h://host:port                 (SOCKS5, DNS resolved through the proxy)

Credentials are URL-decoded. The parsed server form never contains the
credentials, so they can be handed to Playwright's native proxy auth
(handled inside the browser process — they never appear on the Chromium
command line, where any local user or `ps` snapshot could read them).

SOCKS5 notes: the default camoufox engine (Firefox) applies SOCKS5
username/password auth natively; the Chromium engine ignores credentials for
SOCKS proxies (a Chromium limitation) — configure the bridge to allow the
server's IP or use the camoufox engine. ``socks5h`` is normalized to
``socks5`` for the browser (remote DNS is the browser default there), while
the relay shim always resolves the target hostname through the proxy.
"""

from __future__ import annotations

from urllib.parse import unquote, urlparse

# Schemes accepted in BROWSER_PROXY; anything else is refused loudly.
_SUPPORTED_SCHEMES = ("http", "https", "socks5", "socks5h")


class ProxySpecError(ValueError):
    pass


def proxy_scheme(spec: str) -> str:
    """Explicit scheme of the spec ('http' when none given)."""
    raw = (spec or "").strip()
    if "://" in raw:
        scheme = raw.split("://", 1)[0].lower()
        if scheme not in _SUPPORTED_SCHEMES:
            raise ProxySpecError(
                f"unsupported proxy scheme {scheme!r} "
                f"(supported: {', '.join(_SUPPORTED_SCHEMES)})"
            )
        return "socks5" if scheme == "socks5h" else scheme
    return "http"


def proxy_server_url(spec: str) -> str:
    """Full server URL for the browser: ``socks5://host:port`` / ``http://…``.

    Never contains credentials — browsers get them via the proxy auth dict
    (or none at all for SOCKS on Chromium). Raises ProxySpecError on unknown
    schemes or specs without a host.
    """
    server, _, _ = parse_proxy_spec(spec)
    if not server:
        return ""
    return f"{proxy_scheme(spec)}://{server}"


def proxy_parts(spec: str) -> tuple[str, str, int, str | None, str | None]:
    """(scheme, host, port, username, password) — exact parts for dialers."""
    raw = (spec or "").strip()
    if not raw:
        raise ProxySpecError("proxy spec is empty")
    scheme = proxy_scheme(raw)
    if "://" not in raw:
        raw = "http://" + raw
    try:
        u = urlparse(raw)
    except Exception as exc:
        raise ProxySpecError(f"invalid proxy spec: {exc}") from exc
    if not u.hostname:
        raise ProxySpecError("proxy spec missing host")
    port = u.port if u.port else (1080 if scheme.startswith("socks") else 80)
    username = unquote(u.username) if u.username else None
    password = unquote(u.password) if u.password else None
    return scheme, u.hostname, port, username, password


def parse_proxy_spec(spec: str) -> tuple[str, str | None, str | None]:
    """Split a proxy string into (server, username, password).

    ``server`` is ``host:port`` (scheme stripped) suitable for both
    ``--proxy-server`` and Playwright's ``proxy={"server": "http://…"}``.
    """
    raw = (spec or "").strip()
    if not raw:
        return "", None, None
    if "://" not in raw:
        raw = "http://" + raw
    try:
        u = urlparse(raw)
    except Exception as exc:
        raise ProxySpecError(f"invalid proxy spec: {exc}") from exc
    if not u.hostname:
        raise ProxySpecError("proxy spec missing host")
    if ":" in (u.hostname or "") and not u.hostname.startswith("["):
        # Bare IPv6 without brackets would have parsed badly; urlparse only
        # yields bracketed ipv6 hosts correctly.
        pass
    server = f"{u.hostname}:{u.port}" if u.port else str(u.hostname)
    username = unquote(u.username) if u.username else None
    password = unquote(u.password) if u.password else None
    return server, username, password


def playwright_proxy_kwarg(spec: str) -> dict[str, str] | None:
    """Playwright launch(proxy=…) dict for a spec, or None when unset.

    When credentials are present they ride in this dict (Chromium answers
    the proxy's 407 challenge internally; Firefox applies SOCKS5 auth from
    the same fields). Providers like Webshare (user:pass@p.webshare.io:80)
    only work through this path. The scheme is preserved (socks5:// …).
    """
    server, username, password = parse_proxy_spec(spec)
    if not server:
        return None
    d = {"server": f"{proxy_scheme(spec)}://{server}"}
    if username:
        d["username"] = username
    if password:
        d["password"] = password or ""
    return d
