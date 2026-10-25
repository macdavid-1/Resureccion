"""Tests for owner-controlled interactive browser sessions and KDSpy install.

The interactive layer is what lets the owner sign in to Amazon and set up
KDSpy from a phone against the server's persistent profile. These tests use
fakes for the browser (no real browser launch) and verify the durable
guarantees: single session, TTL enforcement, whitelisted actions, atomic
add-on install, and zip-slip/type defense.
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
        self.listeners: dict[str, list[Any]] = {}

    def on(self, event: str, handler: Any) -> None:
        self.listeners.setdefault(event, []).append(handler)

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

    async def new_page(self, *, interactive: bool = False, mobile: bool = True) -> FakePage:
        self.launched += 1
        self.last_mobile = mobile  # what the real manager was asked for
        # Mirror the real BrowserManager: every new_page hands back a FRESH
        # page (the previous context's pages stay closed after a relaunch),
        # and protected pages carry the marker attribute the eviction/busy
        # logic reads.
        page = FakePage()
        page.url = "about:blank"
        if interactive:
            page._resurreccion_protected = True
        page.protected = interactive
        self.page = page
        self._context.pages.append(page)
        return page

    async def navigate(self, page: Any, url: str) -> None:
        self.navigated.append(url)
        page.url = url

    async def close_page(self, page: Any) -> None:
        page.closed = True

    async def apply_mobile_emulation(self, page: Any) -> None:
        self.mobile_emulated = True


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
async def test_signin_opens_on_the_wall_marketplace_not_always_us(
    manager: InteractiveSessionManager,
) -> None:
    """Amazon sessions are per-domain: the sign-in form must open on the
    marketplace that needs the owner. The old US-only URL signed the owner
    into amazon.com even when the agent's wall was on amazon.co.uk, so every
    other marketplace kept reading login_required after a "successful"
    sign-in — the exact "I signed in but the app still says login required"
    report."""
    from app.interactive import AMAZON_SIGNIN_URL, amazon_signin_url
    from app.marketplace import AMAZON_MARKETPLACES

    us = amazon_signin_url(AMAZON_MARKETPLACES["us"])
    uk = amazon_signin_url(AMAZON_MARKETPLACES["uk"])
    assert us == AMAZON_SIGNIN_URL  # the proven US form stays byte-identical
    assert uk.startswith("https://www.amazon.co.uk/ap/signin?")
    assert "return_to=https%3A%2F%2Fwww.amazon.co.uk" in uk  # localized target
    assert "amazon.com" not in uk  # never bounce the owner back to the US store

    # End-to-end through _open_page: the interactive session must actually
    # navigate to the wall's domain.
    s = await manager.start(purpose="amazon_signin", marketplace="uk")
    assert s.status == "open"
    assert "amazon.co.uk" in s.last_url


@pytest.mark.asyncio
async def test_signin_opens_full_pc_viewport_not_phone(
    manager: InteractiveSessionManager,
) -> None:
    """Amazon sign-in opens FULL PC size (the owner zooms/pans/scrolls in
    the viewer); other interactive purposes keep the 1:1 phone layout."""
    await manager.start(purpose="amazon_signin", marketplace="de")
    assert manager.browser.last_mobile is False
    await manager.complete(outcome="completed")

    await manager.start(purpose="kdspy_setup")
    assert manager.browser.last_mobile is True


def test_signin_options_are_seven_with_base_domains() -> None:
    """Settings offers exactly seven differentiated sign-ins, each labeled
    with its real base domain (single source of truth: the catalog)."""
    from app.marketplace import signin_marketplaces

    domains = [m.domain for m in signin_marketplaces()]
    assert domains == [
        "amazon.com",
        "amazon.co.uk",
        "amazon.de",
        "amazon.ca",
        "amazon.com.au",
        "amazon.in",
        "amazon.fr",
    ]


def test_full_pc_viewport_defaults() -> None:
    cfg = Config()
    assert (cfg.browser_viewport_w, cfg.browser_viewport_h) == (1920, 1080)


@pytest.mark.asyncio
async def test_start_labels_navigation_failure_accurately(manager: InteractiveSessionManager) -> None:
    """A navigation failure during start must NOT be mislabeled
    "browser launch failed" (the launch succeeded; the goto failed).

    The mislabel sent the owner hunting for a launch bug while the real
    problem was egress (NS_ERROR_PROXY_CONNECTION_REFUSED)."""
    from app.browser_manager import BrowserManagerError

    async def navigate_fails(self, page: Any, url: str) -> None:
        raise BrowserManagerError(
            f"navigation failed: Page.goto: NS_ERROR_PROXY_CONNECTION_REFUSED "
            f"while opening {url}"
        )

    manager.browser.navigate = navigate_fails.__get__(manager.browser)

    with pytest.raises(InteractiveError, match="page open failed") as ei:
        await manager.start(purpose="kdspy_setup")
    assert "browser launch failed" not in str(ei.value)
    assert "publishingaltitude" in str(ei.value)


@pytest.mark.asyncio
async def test_start_is_idempotent_while_open(manager: InteractiveSessionManager) -> None:
    s1 = await manager.start(purpose="amazon_signin", marketplace="us")
    s2 = await manager.start(purpose="kdspy_setup")
    assert s1.id == s2.id, "a second start must reuse the open session"


@pytest.mark.asyncio
async def test_start_recovers_after_browser_restart_kills_pages(
    manager: InteractiveSessionManager,
) -> None:
    """Regression: the KDSpy install relaunches the browser
    (launch(force=True) tears down the context) BEFORE the client asks for
    the setup browser. That closes an open session's pages — e.g. an Amazon
    sign-in the owner left open. start() used to reuse the corpse without
    checking it, so /frame 404'd forever ("Remote browser is off.") while
    /state still claimed the session was open with time left."""
    s1 = await manager.start(purpose="amazon_signin", marketplace="us")
    # Context teardown closes every page behind the session's back.
    s1.pages[0].closed = True

    s2 = await manager.start(purpose="kdspy_setup")
    assert s2.id != s1.id, "a dead session must never be handed back"
    assert s2.status == "open"
    assert not s2.page.closed
    assert "publishingaltitude" in (s2.last_url or "")  # the KDSpy login page
    # Exactly ONE durable window row is open: the fresh session's.
    open_rows = [
        r for r in manager.windows.list(status="open")
        if getattr(r, "account", None) == "interactive"
    ]
    assert len(open_rows) == 1
    # And frames flow again.
    assert await manager.frame() is not None


@pytest.mark.asyncio
async def test_current_expires_session_whose_pages_died(
    manager: InteractiveSessionManager,
) -> None:
    s = await manager.start(purpose="manual")
    s.page.closed = True  # browser relaunch killed the tab
    cur = manager.current()
    assert cur is not None and cur.status == "expired"
    # Durable row closed too — the owner's UI must not show a login flow
    # that can never come back.
    assert not manager.windows.list(status="open")
    # /frame answers 404 (client shows the message and closes) — no raise.
    assert await manager.frame() is None
    with pytest.raises(InteractiveError, match="expired"):
        await manager.act("key", {"key": "Enter"})


@pytest.mark.asyncio
async def test_ttl_expiry_closes_durable_window_row(
    manager: InteractiveSessionManager,
) -> None:
    s = await manager.start(purpose="manual", ttl_seconds=600)
    s.expires_at = 0  # force TTL expiry
    assert manager.current().status == "expired"
    assert not manager.windows.list(status="open")


@pytest.mark.asyncio
async def test_complete_after_restart_does_not_record_false_success(
    manager: InteractiveSessionManager,
) -> None:
    """Back-tap after the browser relaunched: the session is already dead,
    and completing it must not overwrite the expiry with "completed"."""
    s = await manager.start(purpose="amazon_signin", marketplace="us")
    s.page.closed = True
    done = await manager.complete(outcome="completed")
    assert done.status == "expired"


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
async def test_busy_check_ignores_interactive_tabs(manager: InteractiveSessionManager) -> None:
    """Owner tabs are marked protected and must never read as research work."""
    await manager.start(purpose="amazon_signin", marketplace="us")
    assert not manager.is_research_busy()


@pytest.mark.asyncio
async def test_start_coexists_with_open_research_pages(manager: InteractiveSessionManager) -> None:
    """Regression for the 'research is using the browser' false gate: an
    interactive session must open even while research pages are live — it is
    just another tab in the shared persistent profile."""
    research = FakePage()
    research.url = "https://www.amazon.com/s?k=grief+journal"
    manager.browser._context.pages.append(research)
    s = await manager.start(purpose="amazon_signin", marketplace="us")
    assert s.status == "open"
    # The research page survived the interactive session starting.
    assert research in manager.browser._context.pages
    assert not research.closed


@pytest.mark.asyncio
async def test_complete_never_closes_research_pages(manager: InteractiveSessionManager) -> None:
    """Closing the interactive session must leave research tabs untouched."""
    research = FakePage()
    research.url = "https://www.amazon.com/s?k=x"
    manager.browser._context.pages.append(research)
    await manager.start(purpose="manual")
    await manager.complete(outcome="completed")
    assert manager.browser.page.closed          # interactive tab closed
    assert not research.closed                  # research tab survived
    assert research in manager.browser._context.pages


@pytest.mark.asyncio
async def test_popup_tab_is_tracked_autofocused_and_switchable(
    manager: InteractiveSessionManager,
) -> None:
    """kdspy.com's Login opens its member form in a NEW tab. The session must
    track that tab, auto-focus it (so the stream shows the login form, not
    the stale page), expose it for switching, and close it on complete."""
    s = await manager.start(purpose="kdspy_setup")
    assert s.tabs == ["https://www.kdspy.com/"] if hasattr(s, "tabs") else True
    # The site opens the login popup (real Playwright fires the popup event).
    popup = FakePage()
    popup.url = "https://www.publishingaltitude.com/wp-login.php"
    handlers = manager.browser.page.listeners["popup"]
    for h in handlers:
        h(popup)
    assert len(s.pages) == 2
    assert s.active == 1                      # auto-focused the login tab
    assert "publishingaltitude" in (s.page.url or "")
    # Owner can switch back to the first tab.
    s2 = await manager.act("switch_tab", {"index": 0})
    assert s2.active == 0
    # Completing closes BOTH tabs.
    await manager.complete(outcome="completed")
    assert manager.browser.page.closed
    assert popup.closed
    assert s.pages == []


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
        "background.js": b"// bg",
        "icons/icon.png": b"\x89PNG",
    })
    info = kdspy.install_from_zip(data)
    assert info.name == "KDSpy Pro"
    assert info.version == "4.2.0"
    assert (kdspy.firefox_addon_path / "manifest.json").is_file()
    assert (kdspy.firefox_addon_path / "icons" / "icon.png").is_file()
    state = kdspy.state()
    assert state.status == "installed"


def test_zip_install_wrapped_export_folder(kdspy: KDSpyManager) -> None:
    data = _zip_bytes({
        "kdspy-pro-export/manifest.json": MANIFEST,
        "kdspy-pro-export/background.js": b"",
    })
    info = kdspy.install_from_zip(data)
    assert info.version == "4.2.0"
    assert (kdspy.firefox_addon_path / "manifest.json").is_file()
    # Wrapper folder must be flattened, not nested.
    assert not (kdspy.firefox_addon_path / "kdspy-pro-export").exists()


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
    # Nothing escaped the add-on dir.
    assert not (kdspy.firefox_addon_path.parent.parent / "escape.js").exists()


def test_zip_install_rejects_dangerous_file_types(kdspy: KDSpyManager) -> None:
    data = _zip_bytes({"manifest.json": MANIFEST, "payload.exe": b"MZ..."})
    with pytest.raises(ExtensionInstallError, match="allowed"):
        kdspy.install_from_zip(data)


def test_zip_install_preserves_old_install_on_failure(kdspy: KDSpyManager) -> None:
    good = _zip_bytes({"manifest.json": MANIFEST, "background.js": b"v1"})
    kdspy.install_from_zip(good)
    marker = (kdspy.firefox_addon_path / "background.js").read_bytes()
    bad = _zip_bytes({"manifest.json": b"not json"})
    with pytest.raises(ExtensionInstallError):
        kdspy.install_from_zip(bad)
    # Old install survived intact.
    assert (kdspy.firefox_addon_path / "background.js").read_bytes() == marker


def test_zip_install_rejects_bad_manifest(kdspy: KDSpyManager) -> None:
    data = _zip_bytes({"manifest.json": b'{"name": "", "version": "", "manifest_version": 9}'})
    with pytest.raises(ExtensionInstallError):
        kdspy.install_from_zip(data)


def test_files_install_flat(kdspy: KDSpyManager) -> None:
    files = [
        ("manifest.json", io.BytesIO(MANIFEST)),
        ("background.js", io.BytesIO(b"")),
    ]
    info = kdspy.install_from_files(files)
    assert info.version == "4.2.0"
    assert (kdspy.firefox_addon_path / "background.js").is_file()


def test_files_install_rejects_unsafe_path(kdspy: KDSpyManager) -> None:
    with pytest.raises(ExtensionInstallError, match="unsafe"):
        kdspy.install_from_files([("../x.json", io.BytesIO(b"{}"))])


def test_files_install_requires_manifest(kdspy: KDSpyManager) -> None:
    with pytest.raises(ExtensionInstallError, match="manifest"):
        kdspy.install_from_files([("sw.js", io.BytesIO(b""))])


def test_update_flow_replaces_previous_version(kdspy: KDSpyManager) -> None:
    v1 = _zip_bytes({"manifest.json": MANIFEST, "background.js": b"v1"})
    kdspy.install_from_zip(v1)
    manifest_v2 = MANIFEST.replace(b"4.2.0", b"4.3.1")
    v2 = _zip_bytes({"manifest.json": manifest_v2, "background.js": b"v2"})
    info = kdspy.install_from_zip(v2)
    assert info.version == "4.3.1"
    assert (kdspy.firefox_addon_path / "background.js").read_bytes() == b"v2"
    # The atomic swap leaves no backup/staging dirs behind.
    assert not list(kdspy.firefox_addon_path.parent.glob(".kdspy-firefox-*"))


def test_remove(kdspy: KDSpyManager) -> None:
    kdspy.install_from_zip(_zip_bytes({"manifest.json": MANIFEST}))
    assert kdspy.firefox_addon_is_configured()
    kdspy.remove()
    assert not kdspy.firefox_addon_is_configured()
