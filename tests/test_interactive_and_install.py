"""Tests for owner-controlled interactive browser sessions and KDSpy install.

The interactive layer is what lets the owner sign in to Amazon and set up
KDSpy from a phone against the server's persistent profile. These tests use
fakes for the browser (no real Chromium) and verify the durable guarantees:
single session, TTL enforcement, whitelisted actions, atomic extension
install, and zip-slip/type defense.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any

import pytest

from app.config import Config
from app.interactive import InteractiveError, InteractiveSessionManager
from app.kdspy import ExtensionInstallError, KDSpyManager


# ------------------------------------------------------------------- fakes
class FakeInput:
    async def click(self, x: int, y: int) -> None:
        pass

    async def wheel(self, dx: int, dy: int) -> None:
        pass

    async def type(self, text: str, delay: int = 0) -> None:
        pass

    async def press(self, key: str) -> None:
        pass


class FakePage:
    def __init__(self) -> None:
        self.url = "https://www.amazon.com/"
        self.closed = False
        self.protected = False
        self.mouse = FakeInput()
        self.keyboard = FakeInput()

    async def title(self) -> str:
        return "Amazon"

    async def close(self) -> None:
        self.closed = True

    async def screenshot(self, **_: Any) -> bytes:
        return b"\xff\xd8fake-jpeg"


class FakeContext:
    def __init__(self) -> None:
        self.pages: list[FakePage] = []


class FakeBrowser:
    def __init__(self) -> None:
        self._context = FakeContext()
        self.launched = 0
        self.page = FakePage()
        self.navigated: list[str] = []

    async def open_marketplace(self, marketplace: Any, path: str = "/") -> FakePage:
        self.launched += 1
        self.page.url = f"https://{marketplace.domain}{path}"
        self._context.pages.append(self.page)
        return self.page

    async def new_page(self, *, interactive: bool = False) -> FakePage:
        self.launched += 1
        self.page.protected = interactive
        self._context.pages.append(self.page)
        return self.page

    async def navigate(self, page: Any, url: str) -> None:
        self.navigated.append(url)
        page.url = url

    async def close_page(self, page: Any) -> None:
        page.closed = True


class FakeWindows:
    def __init__(self) -> None:
        self.rows: list[Any] = []

    def create(self, account: str, profile: str, ttl: int) -> Any:
        row = type("W", (), {})()
        row.id = f"w{len(self.rows)}"
        row.account = account
        row.profile = profile
        row.status = "open"
        self.rows.append(row)
        return row

    def list(self, *, status: str | None = None) -> list[Any]:
        return [r for r in self.rows if status is None or r.status == status]

    def set_status(self, window_id: str, status: str, result: dict | None = None) -> None:
        for r in self.rows:
            if r.id == window_id:
                r.status = status


class VoidState:
    status = "installed"

    def to_dict(self) -> dict:
        return {"status": self.status}


class VoidStore:
    """Stand-in for the extension store in install tests."""

    def upsert(self, *a: Any, **k: Any) -> VoidState:
        return VoidState()

    def get(self, *a: Any, **k: Any) -> None:
        return None


# ---------------------------------------------------------------- fixtures
@pytest.fixture()
def manager(tmp_path: Path) -> InteractiveSessionManager:
    cfg = Config()
    cfg.data_dir = tmp_path / "d"
    cfg.browser_login_window_seconds = 900
    cfg.ensure_dirs()
    browser = FakeBrowser()
    mgr = InteractiveSessionManager(cfg, browser, FakeWindows())  # type: ignore[arg-type]
    mgr.browser = browser  # expose for assertions
    return mgr


@pytest.fixture()
def kdspy(tmp_path: Path) -> KDSpyManager:
    cfg = Config()
    cfg.data_dir = tmp_path / "d"
    cfg.ensure_dirs()
    return KDSpyManager(cfg, VoidStore())  # type: ignore[arg-type]


# ------------------------------------------------- interactive sessions
@pytest.mark.asyncio
async def test_start_opens_amazon_page_on_persistent_profile(manager: InteractiveSessionManager) -> None:
    s = await manager.start(purpose="amazon_signin", marketplace="us")
    assert s.status == "open"
    assert "amazon.com" in s.last_url
    assert manager.browser.page.protected  # interactive pages are eviction-safe
    # Durable record exists.
    assert any(r.account == "interactive" for r in manager.windows.rows)


@pytest.mark.asyncio
async def test_start_is_idempotent_while_open(manager: InteractiveSessionManager) -> None:
    s1 = await manager.start(purpose="amazon_signin", marketplace="us")
    s2 = await manager.start(purpose="kdspy_setup")
    assert s1.id == s2.id, "a second start must reuse the open session"


@pytest.mark.asyncio
async def test_act_whitelist_enforced(manager: InteractiveSessionManager) -> None:
    await manager.start(purpose="manual")
    with pytest.raises(InteractiveError):
        await manager.act("shell", {"cmd": "rm -rf /"})
    with pytest.raises(InteractiveError):
        await manager.act("type", {"text": "x" * 900})  # over the cap
    s = await manager.act("key", {"key": "Enter"})
    assert s.status == "open"


@pytest.mark.asyncio
async def test_act_bounds_coordinates(manager: InteractiveSessionManager) -> None:
    await manager.start(purpose="manual")
    s = await manager.act("click", {"x": 999999, "y": "42"})
    assert s.status == "open"  # clamped, not crashed


@pytest.mark.asyncio
async def test_expired_session_refuses_actions(manager: InteractiveSessionManager) -> None:
    s = await manager.start(purpose="manual", ttl_seconds=600)
    s.expires_at = 0  # force expiry
    with pytest.raises(InteractiveError, match="expired"):
        await manager.act("key", {"key": "Enter"})
    assert s.status == "expired"


@pytest.mark.asyncio
async def test_complete_closes_page_and_records_outcome(manager: InteractiveSessionManager) -> None:
    await manager.start(purpose="kdspy_setup")
    assert not manager.browser.page.closed
    s = await manager.complete(outcome="completed")
    assert s.status == "completed"
    assert manager.browser.page.closed
    open_rows = manager.windows.list(status="open")
    assert not open_rows


@pytest.mark.asyncio
async def test_start_after_complete_is_not_blocked(manager: InteractiveSessionManager) -> None:
    """Regression: after a completed session, the persistent context's idle
    about:blank page must not read as 'research is using the browser'."""
    await manager.start(purpose="amazon_signin", marketplace="us")
    await manager.complete(outcome="completed")
    # Simulate the real leftover: the persistent context keeps one idle blank.
    blank = FakePage()
    blank.url = "about:blank"
    manager.browser._context.pages = [blank]
    s2 = await manager.start(purpose="amazon_signin", marketplace="us")
    assert s2.status == "open"


@pytest.mark.asyncio
async def test_busy_check_ignores_idle_blank_pages(manager: InteractiveSessionManager) -> None:
    blank = FakePage()
    blank.url = "about:blank"
    manager.browser._context.pages = [blank]
    assert not manager.is_research_busy()
    real = FakePage()
    real.url = "https://www.amazon.com/s?k=x"
    manager.browser._context.pages.append(real)
    assert manager.is_research_busy()


@pytest.mark.asyncio
async def test_research_busy_refuses_start(manager: InteractiveSessionManager) -> None:
    manager.browser._context = type("Ctx", (), {"pages": [FakePage()]})()
    with pytest.raises(InteractiveError, match="research"):
        await manager.start(purpose="manual")


# ------------------------------------------------------ extension install
def _zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


MANIFEST = b'{"name": "KDSpy Pro", "version": "4.2.0", "manifest_version": 3, "background": {"service_worker": "sw.js"}}'


def test_zip_install_roundtrip(kdspy: KDSpyManager) -> None:
    data = _zip_bytes({
        "manifest.json": MANIFEST,
        "sw.js": b"self.oninstall=()=>{}",
        "icons/icon.png": b"\x89PNG",
    })
    info = kdspy.install_from_zip(data)
    assert info.name == "KDSpy Pro"
    assert info.version == "4.2.0"
    assert (kdspy.extension_path / "manifest.json").is_file()
    assert (kdspy.extension_path / "icons" / "icon.png").is_file()
    state = kdspy.state()
    assert state.status == "installed"


def test_zip_install_wrapped_export_folder(kdspy: KDSpyManager) -> None:
    data = _zip_bytes({
        "kdspy-pro-export/manifest.json": MANIFEST,
        "kdspy-pro-export/sw.js": b"",
    })
    info = kdspy.install_from_zip(data)
    assert info.version == "4.2.0"
    assert (kdspy.extension_path / "manifest.json").is_file()
    # Wrapper folder must be flattened, not nested.
    assert not (kdspy.extension_path / "kdspy-pro-export").exists()


def test_zip_install_rejects_non_zip(kdspy: KDSpyManager) -> None:
    with pytest.raises(ExtensionInstallError, match="ZIP"):
        kdspy.install_from_zip(b"this is not a zip")


def test_zip_install_rejects_missing_manifest(kdspy: KDSpyManager) -> None:
    data = _zip_bytes({"sw.js": b""})
    with pytest.raises(ExtensionInstallError, match="manifest"):
        kdspy.install_from_zip(data)


def test_zip_install_rejects_zip_slip(kdspy: KDSpyManager) -> None:
    data = _zip_bytes({
        "manifest.json": MANIFEST,
        "../escape.js": b"alert(1)",
    })
    with pytest.raises(ExtensionInstallError, match="unsafe|allowed"):
        kdspy.install_from_zip(data)
    # Nothing escaped the extension dir.
    assert not (kdspy.extension_path.parent.parent / "escape.js").exists()


def test_zip_install_rejects_dangerous_file_types(kdspy: KDSpyManager) -> None:
    data = _zip_bytes({"manifest.json": MANIFEST, "payload.exe": b"MZ..."})
    with pytest.raises(ExtensionInstallError, match="allowed"):
        kdspy.install_from_zip(data)


def test_zip_install_preserves_old_install_on_failure(kdspy: KDSpyManager) -> None:
    good = _zip_bytes({"manifest.json": MANIFEST, "sw.js": b"v1"})
    kdspy.install_from_zip(good)
    marker = (kdspy.extension_path / "sw.js").read_bytes()
    bad = _zip_bytes({"manifest.json": b"not json"})
    with pytest.raises(ExtensionInstallError):
        kdspy.install_from_zip(bad)
    # Old install survived intact.
    assert (kdspy.extension_path / "sw.js").read_bytes() == marker


def test_zip_install_rejects_bad_manifest(kdspy: KDSpyManager) -> None:
    data = _zip_bytes({"manifest.json": b'{"name": "", "version": "", "manifest_version": 9}'})
    with pytest.raises(ExtensionInstallError):
        kdspy.install_from_zip(data)


def test_files_install_flat(kdspy: KDSpyManager) -> None:
    files = [
        ("manifest.json", io.BytesIO(MANIFEST)),
        ("sw.js", io.BytesIO(b"")),
    ]
    info = kdspy.install_from_files(files)
    assert info.version == "4.2.0"
    assert (kdspy.extension_path / "sw.js").is_file()


def test_files_install_rejects_unsafe_path(kdspy: KDSpyManager) -> None:
    with pytest.raises(ExtensionInstallError, match="unsafe"):
        kdspy.install_from_files([("../x.json", io.BytesIO(b"{}"))])


def test_files_install_requires_manifest(kdspy: KDSpyManager) -> None:
    with pytest.raises(ExtensionInstallError, match="manifest"):
        kdspy.install_from_files([("sw.js", io.BytesIO(b""))])


def test_update_flow_replaces_previous_version(kdspy: KDSpyManager) -> None:
    v1 = _zip_bytes({"manifest.json": MANIFEST, "sw.js": b"v1"})
    kdspy.install_from_zip(v1)
    manifest_v2 = MANIFEST.replace(b"4.2.0", b"4.3.1")
    v2 = _zip_bytes({"manifest.json": manifest_v2, "sw.js": b"v2"})
    info = kdspy.install_from_zip(v2)
    assert info.version == "4.3.1"
    assert (kdspy.extension_path / "sw.js").read_bytes() == b"v2"
    assert not list(kdspy.extension_path.parent.glob(".kdspy-bak-*"))


def test_remove(kdspy: KDSpyManager) -> None:
    kdspy.install_from_zip(_zip_bytes({"manifest.json": MANIFEST}))
    assert kdspy.is_configured()
    kdspy.remove()
    assert not kdspy.is_configured()
