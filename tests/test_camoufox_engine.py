"""Tests for the Camoufox engine layer (app/camoufox_engine.py).

Live-launch behavior is covered by scripts/camoufox_smoke.py; these tests
exercise the deterministic parts: config gating, kwargs assembly, proxy/relay
selection, fingerprint caching (identity stability + corruption recovery),
and the interactive UA conversion.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import camoufox_engine as ce
from app.config import Config


@pytest.fixture()
def cfg(tmp_path: Path) -> Config:
    c = Config()
    c.data_dir = tmp_path / "data"
    c.browser_profiles_dir = c.data_dir / "browser_profiles"
    c.browser_profiles_dir.mkdir(parents=True, exist_ok=True)
    c.browser_proxy = ""
    c.camoufox_humanize = False
    c.camoufox_geoip = False
    c.browser_headless = True
    return c


requires_camoufox = pytest.mark.skipif(
    not ce.CAMOUFOX_AVAILABLE, reason="camoufox not installed"
)


def test_engine_config_default_is_camoufox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BROWSER_ENGINE", raising=False)
    c = Config()
    assert c.browser_engine == "camoufox"


def test_engine_config_accepts_chromium(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROWSER_ENGINE", "chromium")
    c = Config()
    assert c.browser_engine == "chromium"


def test_engine_config_rejects_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROWSER_ENGINE", "webkit")
    c = Config()
    assert c.browser_engine == "camoufox"


@requires_camoufox
def test_launch_kwargs_shape(cfg: Config) -> None:
    kw = ce.launch_kwargs(cfg)
    assert kw["persistent_context"] is True
    assert kw["headless"] is True
    assert kw["user_data_dir"].endswith("kdspy")
    assert kw["disable_coop"] is True  # cross-origin CAPTCHA iframes clickable
    assert kw["block_webrtc"] is True
    assert "media.peerconnection.enabled" in kw["firefox_user_prefs"]
    assert kw["firefox_user_prefs"]["privacy.globalprivacycontrol.enabled"] is True
    assert "proxy" not in kw  # no relay, no configured proxy
    assert "humanize" not in kw and "geoip" not in kw


@requires_camoufox
def test_fingerprint_is_stable_across_restarts(cfg: Config) -> None:
    fp1 = ce.load_or_generate_fingerprint(cfg)
    fp2 = ce.load_or_generate_fingerprint(cfg)
    assert fp1.navigator.userAgent == fp2.navigator.userAgent
    assert fp1.screen.width == fp2.screen.width
    assert fp1.navigator.platform == fp2.navigator.platform
    assert ce.stable_fingerprint_path(cfg).exists()


@requires_camoufox
def test_fingerprint_cache_corruption_regenerates(cfg: Config) -> None:
    ce.load_or_generate_fingerprint(cfg)  # create the cache
    path = ce.stable_fingerprint_path(cfg)
    path.write_text("{not json", encoding="utf-8")
    fp = ce.load_or_generate_fingerprint(cfg)  # must not raise
    assert fp.navigator.userAgent


@requires_camoufox
def test_fingerprint_cache_wrong_shape_regenerates(cfg: Config) -> None:
    path = ce.stable_fingerprint_path(cfg)
    path.write_text(json.dumps({"junk": True}), encoding="utf-8")
    fp = ce.load_or_generate_fingerprint(cfg)
    assert fp.navigator.userAgent
    # And the cache file was rewritten with the real shape.
    reloaded = json.loads(path.read_text(encoding="utf-8"))
    assert "navigator" in reloaded and "screen" in reloaded


@requires_camoufox
def test_proxy_kwargs_owner_proxy(cfg: Config) -> None:
    cfg.browser_proxy = "user:pass@p.webshare.io:80"
    kw = ce.launch_kwargs(cfg)
    assert kw["proxy"] == {
        "server": "http://p.webshare.io:80",
        "username": "user",
        "password": "pass",
    }


@requires_camoufox
def test_proxy_kwargs_relay_overrides_owner_proxy(cfg: Config) -> None:
    cfg.browser_proxy = "user:pass@p.webshare.io:80"
    kw = ce.launch_kwargs(cfg, relay_addr="127.0.0.1:39999")
    assert kw["proxy"] == {"server": "http://127.0.0.1:39999"}


@requires_camoufox
def test_humanize_and_geoip_flags(cfg: Config) -> None:
    cfg.camoufox_humanize = True
    kw = ce.launch_kwargs(cfg)
    assert kw["humanize"] is True
    cfg.camoufox_geoip = True
    kw2 = ce.launch_kwargs(cfg)
    assert kw2["geoip"] is True


@requires_camoufox
def test_addons_passed_to_launch_kwargs(cfg: Config) -> None:
    """The KDSpy Firefox add-on (extracted dir) rides the camoufox addons
    option; none by default."""
    kw = ce.launch_kwargs(cfg)
    assert "addons" not in kw
    addon_dir = cfg.browser_profiles_dir.parent / "addons" / "kdspy-firefox"
    addon_dir.mkdir(parents=True, exist_ok=True)
    kw2 = ce.launch_kwargs(cfg, addons=[str(addon_dir)])
    assert kw2["addons"] == [str(addon_dir)]


def test_interactive_ua_is_firefox_shaped(cfg: Config) -> None:
    cfg.browser_interactive_user_agent = (
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
    )
    ua = ce.interactive_ua(cfg)
    assert "Firefox" in ua
    assert "Chrome" not in ua and "AppleWebKit" not in ua


def test_interactive_ua_passes_through_explicit_firefox(cfg: Config) -> None:
    explicit = "Mozilla/5.0 (Android 14; Mobile; rv:132.0) Gecko/132.0 Firefox/132.0"
    cfg.browser_interactive_user_agent = explicit
    assert ce.interactive_ua(cfg) == explicit


def test_firefox_ua_helper() -> None:
    ua = ce.firefox_ua("131.0", "windows")
    assert ua.startswith("Mozilla/5.0 (Windows NT 10.0")
    assert "Firefox/131.0" in ua
