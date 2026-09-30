"""Tests for proxy spec parsing (provider-agnostic; Webshare-style specs).

The failure mode this guards: credentials embedded in a proxy server string
are silently ignored by browsers, so a user:pass@host:port proxy (the
standard form for Webshare and most residential providers) must be split
and passed through the launch proxy dict (server + username + password).
"""

from __future__ import annotations

import pytest

from app.proxy_spec import (
    ProxySpecError,
    parse_proxy_spec,
    playwright_proxy_kwarg,
    proxy_parts,
    proxy_scheme,
    proxy_server_url,
)


def test_plain_host_port() -> None:
    server, user, pw = parse_proxy_spec("p.webshare.io:80")
    assert server == "p.webshare.io:80"
    assert user is None and pw is None


def test_user_pass_form_splits_credentials() -> None:
    server, user, pw = parse_proxy_spec("myuser-1:mypassword@p.webshare.io:80")
    assert server == "p.webshare.io:80"
    assert user == "myuser-1"
    assert pw == "mypassword"


def test_scheme_prefix_accepted() -> None:
    server, user, pw = parse_proxy_spec("http://u:p@proxy.example.com:3128")
    assert server == "proxy.example.com:3128"
    assert user == "u" and pw == "p"


def test_url_encoded_credentials_decoded() -> None:
    server, user, pw = parse_proxy_spec("u%40mail.com:p%40ss@host.example:8080")
    assert user == "u@mail.com"
    assert pw == "p@ss"


def test_missing_port_is_allowed() -> None:
    server, _, _ = parse_proxy_spec("proxy.example.com")
    assert server == "proxy.example.com"


def test_empty_spec_is_empty() -> None:
    assert parse_proxy_spec("") == ("", None, None)
    assert parse_proxy_spec("   ") == ("", None, None)


def test_garbage_raises() -> None:
    with pytest.raises(ProxySpecError):
        parse_proxy_spec("http://")


def test_playwright_kwarg_none_when_unset() -> None:
    assert playwright_proxy_kwarg("") is None


def test_playwright_kwarg_includes_credentials() -> None:
    kw = playwright_proxy_kwarg("myuser-1:mypassword@p.webshare.io:80")
    assert kw == {
        "server": "http://p.webshare.io:80",
        "username": "myuser-1",
        "password": "mypassword",
    }


def test_playwright_kwarg_server_has_no_credentials() -> None:
    """The server string that lands in the launch proxy dict must never
    contain the username or password."""
    kw = playwright_proxy_kwarg("secretuser:secretpass@p.webshare.io:80")
    assert "secretuser" not in kw["server"]
    assert "secretpass" not in kw["server"]


# ----------------------------------------------------------------- socks5
def test_socks5_scheme_preserved_in_server_url() -> None:
    """A Cloudflare WARP bridge (socks5:// on the owner's VPS) must keep its
    scheme through the browser launch path — forcing http:// would break it."""
    assert proxy_server_url("socks5://user:pass@vps.example:1080") == "socks5://vps.example:1080"
    kw = playwright_proxy_kwarg("socks5://user:pass@vps.example:1080")
    assert kw == {
        "server": "socks5://vps.example:1080",
        "username": "user",
        "password": "pass",
    }
    assert "user" not in kw["server"] and "pass" not in kw["server"]


def test_socks5h_normalized_to_socks5_for_browsers() -> None:
    """socks5h is a curl-ism (remote DNS); browsers express the same intent
    natively, so normalize the scheme for the launch URL."""
    assert proxy_scheme("socks5h://vps.example:1080") == "socks5"
    assert proxy_server_url("socks5h://vps.example:1080") == "socks5://vps.example:1080"


def test_default_scheme_is_http() -> None:
    assert proxy_scheme("host.example:8080") == "http"
    assert proxy_server_url("host.example:8080") == "http://host.example:8080"


def test_unsupported_scheme_refused() -> None:
    with pytest.raises(ProxySpecError):
        proxy_scheme("ftp://host.example:21")


def test_proxy_parts_exact_fields() -> None:
    scheme, host, port, user, pw = proxy_parts("socks5://warp:s3cret@203.0.113.9:1080")
    assert (scheme, host, port, user, pw) == ("socks5", "203.0.113.9", 1080, "warp", "s3cret")
    # Default ports per family when omitted.
    assert proxy_parts("socks5://vps.example")[1:3] == ("vps.example", 1080)
    assert proxy_parts("host.example:3128")[1:3] == ("host.example", 3128)


def test_proxy_parts_empty_raises() -> None:
    with pytest.raises(ProxySpecError):
        proxy_parts("")



