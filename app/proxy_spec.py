"""Proxy spec parsing shared by the browser launcher and the relay shim.

Accepts the owner-facing forms:
    host:port
    user:pass@host:port
    http://host:port
    http://user:pass@host:port

Credentials are URL-decoded. The parsed server form never contains the
credentials, so they can be handed to Playwright's native proxy auth
(handled inside the browser process — they never appear on the Chromium
command line, where any local user or `ps` snapshot could read them).
"""

from __future__ import annotations

from urllib.parse import unquote, urlparse


class ProxySpecError(ValueError):
    pass


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
    the proxy's 407 challenge internally). Providers like Webshare
    (user:pass@p.webshare.io:80) only work through this path.
    """
    server, username, password = parse_proxy_spec(spec)
    if not server:
        return None
    d = {"server": f"http://{server}"}
    if username:
        d["username"] = username
    if password:
        d["password"] = password or ""
    return d
