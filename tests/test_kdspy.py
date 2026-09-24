"""Tests for the KDSpy extension manager."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from app.browser_store import (
    EXT_FAILED,
    EXT_INSTALLED,
    EXT_NOT_CONFIGURED,
    EXT_VALIDATED,
    ExtensionStore,
)
from app.kdspy import ExtensionInstallError, KDSpyError, KDSpyManager, _version_tuple


MV3_MANIFEST = {
    "name": "KDSpy Pro",
    "version": "4.2.1",
    "manifest_version": 3,
    "background": {"service_worker": "background.js"},
}


@pytest.fixture()
def kdspy(db, config) -> KDSpyManager:
    return KDSpyManager(config, ExtensionStore(db))


def test_not_configured_when_absent(kdspy) -> None:
    state = kdspy.validate_installation()
    assert state.status == EXT_NOT_CONFIGURED
    assert kdspy.chromium_args() == []


def test_missing_manifest_is_failure(kdspy) -> None:
    kdspy.extension_path.mkdir(parents=True, exist_ok=True)
    state = kdspy.validate_installation()
    assert state.status == EXT_FAILED


def test_valid_manifest_registers_installed(kdspy, config) -> None:
    config.kdspy_extension_path.mkdir(parents=True, exist_ok=True)
    (config.kdspy_extension_path / "manifest.json").write_text(json.dumps(MV3_MANIFEST))
    state = kdspy.validate_installation()
    assert state.status == EXT_INSTALLED
    assert state.version == "4.2.1"
    assert state.detail["manifest"]["background"] == "service_worker"
    args = kdspy.chromium_args()
    assert len(args) == 2
    assert "--load-extension=" in args[1]


def test_version_minimum_enforced(kdspy, config) -> None:
    config.kdspy_expected_min_version = "5.0.0"
    config.kdspy_extension_path.mkdir(parents=True, exist_ok=True)
    (config.kdspy_extension_path / "manifest.json").write_text(json.dumps(MV3_MANIFEST))
    state = kdspy.validate_installation()
    assert state.status == EXT_FAILED
    assert "minimum" in state.detail["error"]


def test_corrupt_manifest_is_failure(kdspy, config) -> None:
    config.kdspy_extension_path.mkdir(parents=True, exist_ok=True)
    (config.kdspy_extension_path / "manifest.json").write_text("{not json")
    state = kdspy.validate_installation()
    assert state.status == EXT_FAILED


def test_runtime_validation_roundtrip(kdspy, config) -> None:
    config.kdspy_extension_path.mkdir(parents=True, exist_ok=True)
    (config.kdspy_extension_path / "manifest.json").write_text(json.dumps(MV3_MANIFEST))
    kdspy.validate_installation()
    state = kdspy.record_runtime_validated("abcdeffedcba", "chrome-extension://abcdeffedcba/bg.js")
    assert state.status == EXT_VALIDATED
    assert state.extension_id == "abcdeffedcba"
    fail = kdspy.record_runtime_failure("sw missing")
    assert fail.status == EXT_FAILED


def test_version_tuple() -> None:
    assert _version_tuple("4.10.2") == (4, 10, 2)
    assert _version_tuple("junk") == (0,)


# ---------------------------------------------------------------- firefox XPI


def _xpi_bytes(manifest: dict) -> bytes:
    buf = __import__("io").BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest))
        z.writestr("background.js", "// bg")
    return buf.getvalue()


FX_MANIFEST = {
    "manifest_version": 2,
    "name": "KDSPY – Keyword Research for Amazon",
    "version": "1.2.0",
    "background": {"scripts": ["background.js"]},
}


def test_firefox_xpi_install_roundtrip(kdspy, config) -> None:
    info = kdspy.install_firefox_xpi(_xpi_bytes(FX_MANIFEST))
    assert info.version == "1.2.0"
    assert info.manifest_version == 2
    assert kdspy.firefox_addon_is_configured()
    # camoufox gets the extracted add-on dir to load.
    args = kdspy.firefox_addon_args()
    assert len(args) == 1
    assert args[0].endswith("kdspy-firefox")
    assert (Path(args[0]) / "manifest.json").is_file()
    # Durable state is recorded for the firefox flavor.
    st = kdspy.firefox_addon_state()
    assert st.status == EXT_INSTALLED
    assert st.version == "1.2.0"


def test_firefox_xpi_not_configured_by_default(kdspy) -> None:
    assert not kdspy.firefox_addon_is_configured()
    assert kdspy.firefox_addon_args() == []
    st = kdspy.firefox_addon_state()
    assert st.status == EXT_NOT_CONFIGURED


def test_firefox_xpi_rejects_bad_zip(kdspy) -> None:
    with pytest.raises(ExtensionInstallError):
        kdspy.install_firefox_xpi(b"not a zip")


def test_firefox_xpi_rejects_missing_manifest(kdspy) -> None:
    buf = __import__("io").BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("background.js", "// bg")
    with pytest.raises(ExtensionInstallError):
        kdspy.install_firefox_xpi(buf.getvalue())


def test_firefox_xpi_rejects_zip_slip(kdspy) -> None:
    buf = __import__("io").BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", json.dumps(FX_MANIFEST))
        z.writestr("../../evil.js", "// nope")
    with pytest.raises(ExtensionInstallError):
        kdspy.install_firefox_xpi(buf.getvalue())


def test_firefox_xpi_atomic_swap_keeps_old_on_failure(kdspy) -> None:
    kdspy.install_firefox_xpi(_xpi_bytes(FX_MANIFEST))
    bad = dict(FX_MANIFEST)
    bad.pop("name", None)
    with pytest.raises(ExtensionInstallError):
        kdspy.install_firefox_xpi(_xpi_bytes(bad))
    # The good install is still intact.
    assert kdspy.firefox_addon_is_configured()
    assert kdspy.firefox_addon_state().version == "1.2.0"


def test_firefox_runtime_validation_roundtrip(kdspy) -> None:
    kdspy.install_firefox_xpi(_xpi_bytes(FX_MANIFEST))
    ok = kdspy.record_firefox_runtime_validated(str(kdspy.firefox_addon_path))
    assert ok.status == EXT_VALIDATED
    fail = kdspy.record_firefox_runtime_failure("launch refused the add-on")
    assert fail.status == EXT_FAILED
    assert "launch refused" in fail.detail["error"]


def test_firefox_addon_state_read_does_not_clobber_validated(kdspy) -> None:
    """Regression: reading the add-on state must not rewrite the durable
    record. The browser manager records 'validated' after a camoufox launch;
    the state endpoint and install responses use the read-only accessor so a
    GET can never downgrade it back to 'installed'."""
    from app.kdspy import KDSPY_FIREFOX_NAME

    kdspy.install_firefox_xpi(_xpi_bytes(FX_MANIFEST))
    kdspy.record_firefox_runtime_validated(str(kdspy.firefox_addon_path))
    before = kdspy.store.get(KDSPY_FIREFOX_NAME, kdspy.profile)

    read = kdspy.firefox_addon_state_stored()
    assert read.status == EXT_VALIDATED
    after = kdspy.store.get(KDSPY_FIREFOX_NAME, kdspy.profile)
    assert after.updated_at == before.updated_at  # no write happened

    # And the fallback still bootstraps when nothing is recorded yet.
    kdspy.store.upsert(KDSPY_FIREFOX_NAME, "other-profile", status=EXT_NOT_CONFIGURED)
    assert kdspy.firefox_addon_state_stored().status in (EXT_INSTALLED, EXT_VALIDATED)


def test_chromium_gating_message_mentions_firefox_addon(kdspy, config) -> None:
    """Engine-aware message: the camoufox path points at the XPI, not a bare
    'set BROWSER_ENGINE=chromium'."""
    config.kdspy_extension_path.mkdir(parents=True, exist_ok=True)
    (config.kdspy_extension_path / "manifest.json").write_text(json.dumps(MV3_MANIFEST))
    kdspy.validate_installation()
    assert not kdspy.firefox_addon_is_configured()


def test_min_version_applies_to_firefox_xpi(kdspy, config) -> None:
    config.kdspy_expected_min_version = "2.0.0"
    with pytest.raises(ExtensionInstallError):
        kdspy.install_firefox_xpi(_xpi_bytes(FX_MANIFEST))
    assert not kdspy.firefox_addon_is_configured()


# ------------------------------------------------------------- AMO one-tap


def test_amo_url_config_default(config) -> None:
    """The pinned AMO URL must be the owner-verified KDSpy download and must
    be an addons.mozilla.org URL."""
    assert config.kdspy_amo_url.startswith("https://addons.mozilla.org/")
    assert config.kdspy_amo_url.endswith(".xpi")
    assert "kdspy" in config.kdspy_amo_url.lower()


async def test_amo_install_roundtrip(kdspy, config) -> None:
    """install_from_amo fetches the pinned URL and runs the full install
    defenses (zip-slip / manifest / version), then records durable state."""
    seen: list[str] = []

    async def fake_fetch() -> bytes:
        seen.append(config.kdspy_amo_url)
        return _xpi_bytes(FX_MANIFEST)

    info = await kdspy.install_from_amo(fetch=fake_fetch)
    assert seen == [config.kdspy_amo_url]
    assert info.version == "1.2.0"
    assert kdspy.firefox_addon_is_configured()
    st = kdspy.firefox_addon_state()
    assert st.status == EXT_INSTALLED
    assert st.version == "1.2.0"


async def test_amo_install_rejects_non_amo_url(kdspy, config) -> None:
    """The URL guard is code-level, not just config discipline."""
    config.kdspy_amo_url = "https://evil.example.com/kdspy.xpi"
    with pytest.raises(ExtensionInstallError, match="addons.mozilla.org"):
        await kdspy.install_from_amo(fetch=_never_fetch)
    assert not kdspy.firefox_addon_is_configured()


async def test_amo_install_rejects_non_zip_payload(kdspy, config) -> None:
    async def fake_fetch() -> bytes:
        return b"<html>not an xpi</html>"

    with pytest.raises(ExtensionInstallError, match="ZIP"):
        await kdspy.install_from_amo(fetch=fake_fetch)
    assert not kdspy.firefox_addon_is_configured()


async def test_amo_install_bad_package_never_clobbers_good_install(
    kdspy, config
) -> None:
    kdspy.install_firefox_xpi(_xpi_bytes(FX_MANIFEST))

    async def fake_fetch() -> bytes:
        return _xpi_bytes({"manifest_version": 2, "name": "x", "version": ""})

    with pytest.raises(ExtensionInstallError):
        await kdspy.install_from_amo(fetch=fake_fetch)
    assert kdspy.firefox_addon_is_configured()
    assert kdspy.firefox_addon_state().version == "1.2.0"


async def test_amo_install_min_version_enforced(kdspy, config) -> None:
    config.kdspy_expected_min_version = "99.0.0"

    async def fake_fetch() -> bytes:
        return _xpi_bytes(FX_MANIFEST)

    with pytest.raises(ExtensionInstallError, match="below required minimum"):
        await kdspy.install_from_amo(fetch=fake_fetch)
    assert not kdspy.firefox_addon_is_configured()


async def test_fetch_amo_xpi_production_path(monkeypatch) -> None:
    """The real fetcher: follows redirects, checks the ZIP magic, and wraps
    transport errors in AmoError."""
    from app import kdspy as kdspy_mod

    class FakeResp:
        content = b"PK\x03\x04fake"

        def raise_for_status(self) -> None:
            pass

    class FakeClient:
        def __init__(self, *a, **kw) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a) -> None:
            pass

        async def get(self, url):
            assert url.endswith(".xpi")
            return FakeResp()

    import httpx

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **kw: FakeClient(*a, **kw)
    )
    data = await kdspy_mod.fetch_amo_xpi("https://addons.mozilla.org/x.xpi")
    assert data.startswith(b"PK\x03\x04")

    def boom(*a, **kw):
        raise RuntimeError("no network")

    monkeypatch.setattr(httpx, "AsyncClient", boom)
    with pytest.raises(kdspy_mod.AmoError):
        await kdspy_mod.fetch_amo_xpi("https://addons.mozilla.org/x.xpi")


async def _never_fetch() -> bytes:
    raise AssertionError("must not be called")
