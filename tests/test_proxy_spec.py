"""Tests for proxy spec parsing (provider-agnostic; Webshare-style specs).

The failure mode this guards: credentials embedded in --proxy-server are
silently ignored by Chromium, so a user:pass@host:port proxy (the standard
form for Webshare and most residential providers) must be split and passed
through Playwright's native proxy auth instead.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.proxy_spec import ProxySpecError, parse_proxy_spec, playwright_proxy_kwarg


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
    """The server string that lands on the Chromium command line must never
    contain the username or password."""
    kw = playwright_proxy_kwarg("secretuser:secretpass@p.webshare.io:80")
    assert "secretuser" not in kw["server"]
    assert "secretpass" not in kw["server"]


def test_browser_manager_uses_native_auth_with_credentials(monkeypatch, tmp_path) -> None:
    """Regression: launch args carry only the host:port; credentials ride in
    launch_kwargs['proxy']."""
    from app.browser_manager import BrowserManager

    captured: dict = {}

    class FakeKD:
        def chromium_args(self):
            return []

        def is_configured(self):
            return False

    class FakeCtx:
        pages: list = []

        def on(self, *a):
            pass

    class FakePW:
        chromium = None

    class FakePlaywrightMod:
        async def start(self):
            class _Chromium:
                async def launch_persistent_context(self, **kwargs):
                    captured.update(kwargs)
                    return FakeCtx()

            fake_pw = FakePW()
            fake_pw.chromium = _Chromium()
            return fake_pw

    import app.browser_manager as bm_mod

    monkeypatch.setattr(bm_mod, "PLAYWRIGHT_AVAILABLE", True)
    monkeypatch.setattr(bm_mod, "async_playwright", lambda: FakePlaywrightMod())

    cfg = SimpleNamespace(
        browser_headless=True,
        browser_no_sandbox=True,
        browser_extra_args=[],
        browser_locale="en-US",
        browser_timezone="UTC",
        browser_user_agent="",
        browser_profiles_dir=tmp_path / "profiles",
        browser_channel="chromium",
        browser_proxy="secretuser:secretpass@p.webshare.io:80",
        browser_max_open_pages=2,
        browser_default_timeout_seconds=5,
        browser_interactive_viewport_w=390,
        browser_interactive_viewport_h=844,
        browser_interactive_user_agent="UA",
        browser_downloads_dir=tmp_path / "dl",
    )

    mgr = BrowserManager(cfg, FakeKD(), evidence=object(), relay=None)
    import asyncio

    async def run():
        await mgr.launch()

    asyncio.run(run())

    args = captured["args"]
    assert any(a.startswith("--proxy-server=http://p.webshare.io:80") for a in args)
    assert not any("secretuser" in a for a in args)
    assert captured["proxy"]["username"] == "secretuser"
    assert captured["proxy"]["password"] == "secretpass"
