"""Proxy spec parsing shared by the browser launcher and the relay shim.

The supported egress shape is the owner's own Cloudflare WARP bridge on a
VPS (see scripts/vps_proxy_setup.sh): a SOCKS5 or CONNECT front that tunnels
into WARP. Accepts the owner-facing forms:
    host:port
    user:pass@host:port
    http://host:port
    http://user:pass@host:port
    socks5://user:pass@host:port        (SOCKS5, e.g. a Cloudflare WARP bridge)
    socks5h://host:port                 (SOCKS5, DNS resolved through the proxy)

Credentials are URL-decoded. The parsed server form never contains the
credentials, so they can be handed to Playwright's native proxy auth
(handled inside the browser process — they never appear in a command line,
where any local user or `ps` snapshot could read them).

SOCKS5 notes: the camoufox engine (Firefox) applies SOCKS5
username/password auth natively. ``socks5h`` is normalized to
``socks5`` for the browser (remote DNS is the browser default there), while
the relay shim always resolves the target hostname through the proxy.
"""

from __future__ import annotations

import asyncio
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

    Never contains credentials — browsers get them via the proxy auth dict.
    Raises ProxySpecError on unknown schemes or specs without a host.
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


# Navigation error signatures that mean "the configured proxy itself is
# unreachable or refusing connections" — NOT ordinary site/network failures.
# Firefox (camoufox) reports NS_ERROR_*; other browsers net::ERR_* (kept for
# classification robustness).
PROXY_NAV_ERRORS = (
    "NS_ERROR_PROXY_CONNECTION_REFUSED",
    "NS_ERROR_PROXY_CONNECTION_FAILURE",
    "NS_ERROR_UNKNOWN_PROXY_HOST",
    "net::ERR_PROXY_CONNECTION_FAILED",
    "net::ERR_TUNNEL_CONNECTION_FAILED",
    "net::ERR_SOCKS_CONNECTION_FAILED",
)


def is_proxy_nav_error(message: str) -> bool:
    """True when a Playwright navigation error means the proxy died.

    Deliberately conservative: falling back on ANY network error would mask
    real site failures and could silently bypass an owner's no-datacenter-IP
    egress policy. Only proxy-specific failures qualify.
    """
    m = message or ""
    return any(sig in m for sig in PROXY_NAV_ERRORS)


async def probe_proxy(spec: str, timeout: float = 6.0) -> tuple[bool, str]:
    """Functional health probe of the proxy endpoint. Returns (usable, detail).

    This is the launch-time gate that keeps a dead BROWSER_PROXY from killing
    every navigation with NS_ERROR_PROXY_CONNECTION_REFUSED: an unusable
    proxy falls back to direct egress instead (policy permitting).

    Functional, not cosmetic: a bare TCP connect proves only that something
    listens — a proxy whose credentials are wrong or whose egress is dead
    would still pass and then break every navigation. The probe completes a
    REAL protocol exchange:

    - ``http``/``https`` proxy → CONNECT to a well-known host; a ``407``
      response means the credentials are wrong (or auth is required but not
      configured) and the proxy is NOT usable; any 2xx passes. The tunnel is
      closed immediately — no site payload is fetched.
    - ``socks5`` → SOCKS5 greeting + chosen-auth reply; method 0xFF (no
      acceptable auth) or an unexpected reply means not usable.

    Credentials from the spec are used for the handshake exactly as the
    browser would, but are never included in the returned detail.
    """
    try:
        scheme, host, port, username, password = proxy_parts(spec)
    except ProxySpecError as exc:
        return False, f"invalid proxy spec: {exc}"
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except Exception as exc:
        return False, f"cannot reach proxy {host}:{port}: {type(exc).__name__}"

    try:
        if scheme in ("http", "https"):
            # CONNECT through the proxy; auth rides Proxy-Authorization (Basic)
            # when the spec carries credentials — the same shape the browser
            # uses (Playwright answers the 407 internally).
            # Dial a real, stable, dialable host so the probe exercises the
            # proxy's full egress path (a proxy that cannot resolve/dial the
            # public internet is not usable either).
            req = f"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n"
            if username is not None:
                import base64

                token = base64.b64encode(f"{username}:{password or ''}".encode()).decode()
                req += f"Proxy-Authorization: Basic {token}\r\n"
            req += "\r\n"
            writer.write(req.encode())
            await writer.drain()
            head = await asyncio.wait_for(reader.readline(), timeout=timeout)
            if not head:
                return False, f"proxy {host}:{port} closed the connection on CONNECT"
            line = head.decode("latin-1", "replace")
            parts = line.split()
            code = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
            if 200 <= code < 300:
                return True, f"{scheme} {host}:{port} CONNECT OK"
            if code == 407:
                # Some proxies demand auth only on real destinations and
                # answer the probe request differently per host; retry once
                # against a second well-known host before declaring the
                # credentials bad — a false "bad credentials" verdict would
                # silently demote a working egress to direct.
                req2 = f"CONNECT www.google.com:443 HTTP/1.1\r\nHost: www.google.com:443\r\n"
                if username is not None:
                    import base64

                    token = base64.b64encode(f"{username}:{password or ''}".encode()).decode()
                    req2 += f"Proxy-Authorization: Basic {token}\r\n"
                req2 += "\r\n"
                try:
                    writer2 = None
                    reader2, writer2 = await asyncio.wait_for(
                        asyncio.open_connection(host, port), timeout=timeout
                    )
                    writer2.write(req2.encode())
                    await writer2.drain()
                    head2 = await asyncio.wait_for(reader2.readline(), timeout=timeout)
                    line2 = (head2 or b"").decode("latin-1", "replace")
                    parts2 = line2.split()
                    code2 = int(parts2[1]) if len(parts2) >= 2 and parts2[1].isdigit() else 0
                    if 200 <= code2 < 300:
                        return True, f"{scheme} {host}:{port} CONNECT OK"
                    if code2 == 407:
                        return False, (
                            f"proxy {host}:{port} rejected the credentials (407) — "
                            "check BROWSER_PROXY username/password"
                        )
                    return False, f"proxy {host}:{port} refused CONNECT (HTTP {code2 or line2.strip()})"
                except Exception as exc2:
                    return False, f"proxy {host}:{port} 407-check failed: {type(exc2).__name__}"
                finally:
                    try:
                        if writer2 is not None:
                            writer2.close()
                    except Exception:
                        pass
            return False, f"proxy {host}:{port} refused CONNECT (HTTP {code or line.strip()})"
        if scheme == "socks5":
            # Greeting: offer username/password (0x02) and no-auth (0x00).
            methods = b"\x02\x00" if username is not None else b"\x00"
            writer.write(b"\x05\x01" + methods)
            await writer.drain()
            reply = await asyncio.wait_for(reader.readexactly(2), timeout=timeout)
            ver, method = reply[0], reply[1]
            if ver != 5:
                return False, f"proxy {host}:{port} is not speaking SOCKS5 (version {ver})"
            if method == 0xFF:
                return False, (
                    f"proxy {host}:{port} rejected every auth method (0xFF) — "
                    "check BROWSER_PROXY credentials"
                )
            if username is not None and method == 2:
                u = (username or "").encode()
                p = (password or "").encode()
                writer.write(bytes([1, len(u)]) + u + bytes([len(p)]) + p)
                await writer.drain()
                auth_reply = await asyncio.wait_for(reader.readexactly(2), timeout=timeout)
                if auth_reply[1] != 0:
                    return False, (
                        f"proxy {host}:{port} rejected the SOCKS5 username/password "
                        f"(status {auth_reply[1]})"
                    )
            return True, f"socks5 {host}:{port} handshake OK"
        return False, f"unsupported proxy scheme {scheme!r}"
    except Exception as exc:
        return False, f"proxy {host}:{port} unusable: {type(exc).__name__}"
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


def playwright_proxy_kwarg(spec: str) -> dict[str, str] | None:
    """Playwright launch(proxy=…) dict for a spec, or None when unset.

    When credentials are present they ride in this dict (browsers answer
    the proxy's 407 challenge internally; Firefox applies SOCKS5 auth from
    the same fields). The scheme is preserved (socks5:// …).
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
