"""Scrivy Book Scout: BSR on Amazon search results, read from the real add-on.

Amazon prints a book's rank only on its product page. The Book Scout add-on
(Scrivy, addons.mozilla.org) reads each result's page and injects a badge with
the rank — which turns one search capture into a ranked field instead of ten
page visits. These tests pin the contract:

- the add-on installed is the vendor's real package, validated exactly like
  KDSpy's (zip-slip, file types, manifest, minimum version);
- it loads in the SAME browser as KDSpy (both addons at launch);
- the capture reads ONLY what the add-on rendered, keeps its own strings, and
  labels its sales/revenue as estimates;
- an absent or empty add-on is a precise failure, never an invented rank;
- the model is allowed to run it wherever a search is allowed.
"""
from __future__ import annotations

import io
import json
import zipfile
from typing import Any

import pytest

from app.book_scout import (
    BOOK_SCOUT_ADDON_NAME,
    BookScoutManager,
    parse_badge,
    parse_page_payload,
    parse_table_row,
)
from app.browser_store import BrowserEvidenceStore, ExtensionStore
from app.claims import ESTIMATE_KINDS, evidence_quality_from_kind, is_estimate_kind
from app.config import Config
from app.events import EventLog
from app.evidence_capture import (
    EvidenceCaptureError,
    EvidenceCollector,
    read_book_scout,
)

# --------------------------------------------------------------------- fakes
MANIFEST = {
    "manifest_version": 3,
    "name": "Scrivy Book Scout — KDP research & tracking",
    "version": "0.1.2",
    "permissions": ["storage", "alarms"],
    "background": {"scripts": ["background.js"]},
    "content_scripts": [{
        "matches": ["https://*.amazon.com/*"],
        "js": ["content/amazon.js"],
        "css": ["content/amazon.css"],
    }],
}


def _xpi(manifest: dict[str, Any] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest or MANIFEST))
        z.writestr("background.js", "// bg")
        z.writestr("content/amazon.js", "// content")
        z.writestr("content/amazon.css", ".scrivy-badge{color:gold}")
        z.writestr("META-INF/mozilla.rsa", b"\x00sig")
    return buf.getvalue()


def _manager(tmp_path, monkeypatch, db) -> BookScoutManager:
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BOOK_SCOUT_FIREFOX_PATH", str(tmp_path / "ext" / "scrivy"))
    from app import config as config_mod

    config_mod.get_config.cache_clear()
    cfg = Config()
    cfg.ensure_dirs()
    return BookScoutManager(cfg, ExtensionStore(db))


# A page whose `.scrivy-badge` DOM the add-on rendered (its own classes and
# text), exactly the shape `_extract_book_scout` reads.
def _badge_payload(*, unavailable: bool = False) -> dict[str, Any]:
    return {
        "url": "https://www.amazon.com/s?k=grief+memoir&i=stripbooks",
        "query": "grief memoir",
        "badges": [
            {
                "asin": "B0AAA11111",
                "text": "Scrivy BSR #1,234 Est. sales ~120/mo Est. revenue ~$1.1K/mo Indie",
                "title": "The Grief Memoir",
                "stats": [
                    {"label": "BSR", "value": "#1,234"},
                    {"label": "Est. sales", "value": "~120/mo"},
                    {"label": "Est. revenue", "value": "~$1.1K/mo"},
                ],
                "indie": True,
                "unavailable": False,
            },
            {
                "asin": "B0BBB22222",
                "text": "Scrivy Rank unavailable" if unavailable else "Scrivy BSR #44,000 Est. sales ~2/mo",
                "title": "Another Book",
                "stats": [] if unavailable else [{"label": "BSR", "value": "#44,000"}],
                "indie": False,
                "unavailable": unavailable,
            },
        ],
        "niche": {
            "text": "Scrivy Niche snapshot Books analyzed 2 Median BSR #22,617",
            "stats": [
                {"label": "Books analyzed", "value": "2"},
                {"label": "Median BSR", "value": "#22,617"},
                {"label": "Est. revenue", "value": "~$1.3K/mo"},
                {"label": "Indie", "value": "1"},
            ],
            "rows": [{
                "asin": "B0AAA11111",
                "cells": ["The Grief Memoir", "A. Author", "B0AAA11111", "#1,234",
                          "~120", "$8.99", "~$1.1K", "412", "✓", "—", "…", ""],
                "title": "The Grief Memoir",
            }],
        },
    }


class _Page:
    def __init__(self, payloads: list[dict[str, Any] | None]) -> None:
        self.payloads = list(payloads)
        self.calls = 0
        self.url = "https://www.amazon.com/s?k=grief+memoir"

    async def evaluate(self, _script: str) -> Any:
        self.calls += 1
        if len(self.payloads) > 1:
            return self.payloads.pop(0)
        return self.payloads[0] if self.payloads else None


class _Browser:
    def __init__(self, page: Any) -> None:
        self.page = page
        self.opened: list[tuple[str, str]] = []
        self.closed: list[Any] = []

    async def open_marketplace(self, marketplace: Any, path: str = "/") -> Any:
        self.opened.append((marketplace.code, path))
        return self.page

    async def new_page(self) -> Any:
        return self.page

    async def navigate(self, page: Any, url: str) -> Any:
        page.url = url
        return page

    async def close_page(self, page: Any) -> None:
        self.closed.append(page)

    async def screenshot(self, page: Any) -> bytes:
        return b"png"

    async def shutdown(self) -> None:
        return None


class _Artifacts:
    def save_bytes(self, *a: Any, **k: Any) -> Any:
        return type("A", (), {"id": "art1"})()


class _Auth:
    profile = "kdspy"
    auth_store = None

    async def _classify_page(self, page: Any, marketplace: Any) -> Any:
        from app.amazon_auth import AuthCheck

        return AuthCheck("unknown", page.url, "t", {})


# --------------------------------------------------------------- the add-on
def test_install_from_amo_installs_the_signed_package(tmp_path, monkeypatch, db):
    mgr = _manager(tmp_path, monkeypatch, db)

    async def fetch() -> bytes:
        return _xpi()

    import asyncio

    info = asyncio.run(mgr.install_from_amo(fetch=fetch))
    assert info.version == "0.1.2"
    assert mgr.firefox_addon_is_configured()
    assert mgr.firefox_addon_args() == [str(mgr.firefox_addon_path)]
    state = mgr.firefox_addon_state_stored()
    assert state.status in ("installed", "validated")
    assert state.version == "0.1.2"
    assert state.name == BOOK_SCOUT_ADDON_NAME
    # Real package files made it in; the signature metadata is allowed.
    assert (mgr.firefox_addon_path / "content" / "amazon.js").is_file()
    assert (mgr.firefox_addon_path / "META-INF" / "mozilla.rsa").is_file()


def test_amo_url_must_be_mozilla_and_min_version_is_enforced(tmp_path, monkeypatch, db):
    mgr = _manager(tmp_path, monkeypatch, db)
    import asyncio

    mgr.config.book_scout_amo_url = "https://evil.example/scrivy.xpi"
    with pytest.raises(Exception, match="addons.mozilla.org"):
        asyncio.run(mgr.install_from_amo())

    mgr.config.book_scout_amo_url = "https://addons.mozilla.org/firefox/downloads/x.xpi"
    mgr.config.book_scout_expected_min_version = "9.9.9"
    with pytest.raises(Exception, match="below required minimum"):
        mgr.install_firefox_xpi(_xpi())


def test_default_amo_url_is_the_pinned_addon() -> None:
    cfg = Config()
    assert cfg.book_scout_amo_url.startswith("https://addons.mozilla.org/")
    assert cfg.book_scout_amo_url.endswith(".xpi")
    assert "scrivy-book-scout" in cfg.book_scout_amo_url
    assert cfg.book_scout_firefox_path.name == "scrivy-book-scout"


def test_both_addons_load_in_the_same_browser(tmp_path, monkeypatch, db):
    """KDSpy and Book Scout are independent add-ons in one browser."""
    from app import browser_manager as bm
    from app.kdspy import KDSpyManager

    mgr = _manager(tmp_path, monkeypatch, db)
    mgr.install_firefox_xpi(_xpi())
    kd = KDSpyManager(mgr.config, ExtensionStore(db))
    # KDSpy isn't installed here; the manager is only for the combined launch.
    monkeypatch.setattr(bm.camoufox_engine, "addons_supported", lambda *a, **k: True)
    browser = bm.BrowserManager(mgr.config, kd, None, book_scout=mgr)
    assert browser._addon_args() == [str(mgr.firefox_addon_path)]
    browser._validate_addons_on_camoufox()
    status = browser.status()
    assert status.book_scout_loaded is True
    assert status.kdspy_loaded is False
    assert mgr.firefox_addon_state_stored().status == "validated"


# ------------------------------------------------------------------ parsing
def test_parse_badge_keeps_the_addons_own_strings():
    raw = _badge_payload()["badges"][0]
    badge = parse_badge(raw)
    assert badge["asin"] == "B0AAA11111"
    assert badge["bsr"] == "#1,234"
    assert badge["indie"] is True
    assert badge["unavailable"] is False
    assert {"label": "Est. sales", "value": "~120/mo"} in badge["stats"]


def test_parse_badge_marks_unavailable_instead_of_inventing_a_rank():
    unavailable = parse_badge(_badge_payload(unavailable=True)["badges"][1])
    assert unavailable["bsr"] == ""
    assert unavailable["unavailable"] is True


def test_parse_table_row_lifts_the_printed_rank_and_keeps_cells():
    payload = _badge_payload()
    row = parse_table_row(payload["niche"]["rows"][0])
    assert row["asin"] == "B0AAA11111"
    assert row["bsr"] == "#1,234"
    assert row["title"] == "The Grief Memoir"
    assert row["cells"][2] == "B0AAA11111"


def test_parse_page_payload_counts_what_was_measured():
    data = parse_page_payload(_badge_payload())
    assert data["counts"] == {"badges": 2, "with_bsr": 2, "unavailable": 0, "indie": 1}
    assert data["niche"]["stats"][1] == {"label": "Median BSR", "value": "#22,617"}
    assert data["niche"]["rows"][0]["bsr"] == "#1,234"
    assert "ESTIMATES" in data["note"]


# ----------------------------------------------------------------- capture
@pytest.mark.asyncio
async def test_capture_waits_for_the_addons_async_badges(db, tmp_path):
    """Badges start as 'Fetching rank…' and fill in one at a time."""
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO research_sessions (id, name, status, mode, created_at, updated_at,"
            " last_activity_at) VALUES ('s1', 'n', 'running', 'auto', '2025-01-01',"
            " '2025-01-01', '2025-01-01')"
        )
    store = BrowserEvidenceStore(db)
    payload = _badge_payload()
    # 1) add-on not there yet, 2) badges injected but empty, 3) filled.
    page = _Page([
        {"badges": [], "niche": {"stats": []}},
        {
            "badges": [
                {"asin": "B0AAA11111", "text": "Scrivy Fetching rank…", "stats": [],
                 "unavailable": True},
            ],
            "niche": {"stats": []},
        },
        payload,
    ])
    collector = EvidenceCollector(_Browser(page), store, _Artifacts())
    captured = await collector.capture_book_scout(
        page, session_id="s1", marketplace=_market(), query="grief memoir",
        wait_seconds=5.0, screenshot=False,
    )
    assert page.calls >= 3
    record = store.get(captured.record.id)
    assert record.kind == "book_scout_scan"
    assert record.data["counts"]["with_bsr"] == 2
    assert record.data["query"] == "grief memoir"
    assert record.data["badges"][0]["bsr"] == "#1,234"


@pytest.mark.asyncio
async def test_capture_fails_precisely_when_the_addon_is_absent():
    page = _Page([{"badges": [], "niche": {"stats": []}}])
    with pytest.raises(EvidenceCaptureError, match="not present on this page"):
        await read_book_scout(page, wait_seconds=0.2)


@pytest.mark.asyncio
async def test_read_book_scout_normalises_a_real_page():
    data = await read_book_scout(_Page([_badge_payload()]), wait_seconds=0.2)
    assert data["addon"] == BOOK_SCOUT_ADDON_NAME
    assert data["counts"]["badges"] == 2


def _market() -> Any:
    from app.marketplace import get_marketplace

    return get_marketplace("us")


# ------------------------------------------------------------------- action
@pytest.mark.asyncio
async def test_the_action_opens_a_books_search_and_captures_badges(db, tmp_path):
    from app.actions import ActionExecutor
    from app.marketplace import AMAZON_MARKETPLACES

    cfg = Config()
    page = _Page([_badge_payload()])
    browser = _Browser(page)
    store = BrowserEvidenceStore(db)
    collector = EvidenceCollector(browser, store, _Artifacts())
    executor = ActionExecutor(
        cfg, browser, collector, _Auth(), EventLog(db)  # type: ignore[arg-type]
    )
    executor.set_marketplaces([AMAZON_MARKETPLACES["us"]])
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO research_sessions (id, name, status, mode, created_at, updated_at,"
            " last_activity_at) VALUES ('s1', 'n', 'running', 'auto', '2025-01-01',"
            " '2025-01-01', '2025-01-01')"
        )
    result = await executor.execute(
        session_id="s1", action="book_scout_scan",
        args={"query": "grief memoir", "screenshot": False, "wait_seconds": 1},
    )
    assert result.ok, result.error
    assert browser.opened[-1][0] == "us"
    assert "i=stripbooks" in browser.opened[-1][1]
    assert "2/2 results carry a BSR" in result.summary
    assert "Median BSR #22,617" in result.summary
    rec = store.get(result.evidence_ids[0])
    assert rec.kind == "book_scout_scan"
    assert browser.closed == [page]


@pytest.mark.asyncio
async def test_the_action_reports_a_missing_addon_and_points_at_product_pages(db, tmp_path):
    from app.actions import ActionExecutor
    from app.marketplace import AMAZON_MARKETPLACES

    cfg = Config()
    page = _Page([{"badges": [], "niche": {"stats": []}}])
    browser = _Browser(page)
    executor = ActionExecutor(
        cfg, browser, EvidenceCollector(browser, BrowserEvidenceStore(db), _Artifacts()),
        _Auth(), EventLog(db),  # type: ignore[arg-type]
    )
    executor.set_marketplaces([AMAZON_MARKETPLACES["us"]])
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO research_sessions (id, name, status, mode, created_at, updated_at,"
            " last_activity_at) VALUES ('s1', 'n', 'running', 'auto', '2025-01-01',"
            " '2025-01-01', '2025-01-01')"
        )
    result = await executor.execute(
        session_id="s1", action="book_scout_scan",
        args={"query": "grief memoir", "screenshot": False, "wait_seconds": 0.2},
    )
    assert not result.ok
    assert "product page" in (result.error or "")
    assert result.evidence_ids == []


# ------------------------------------------------------------- the ledger
def test_book_scout_evidence_is_an_estimate_not_an_observation():
    assert "book_scout_scan" in ESTIMATE_KINDS
    assert is_estimate_kind("book_scout_scan")
    assert evidence_quality_from_kind("book_scout_scan") == "extension_derived"


def test_every_search_phase_may_run_it():
    from app.methodology import SPECS

    for key, spec in SPECS.items():
        if "search_marketplace" in spec.allowed_actions:
            assert "book_scout_scan" in spec.allowed_actions, key


# -------------------------------------------------------------------- routes
@pytest.fixture()
def api_client(monkeypatch, tmp_path):
    from app.main import create_app
    from app.security import hash_password

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OWNER_USERNAME", "owner")
    monkeypatch.setenv("OWNER_PASSWORD_HASH", hash_password("hunter2"))
    monkeypatch.setenv("AUTH_SECRET", "unit-test-secret")
    from app import config as config_mod

    config_mod.get_config.cache_clear()
    from app import db as db_mod

    db_mod._db = None
    from fastapi.testclient import TestClient

    app = create_app()
    with TestClient(app) as c:
        yield c


def _login(client) -> dict:
    token = client.post(
        "/api/auth/login", json={"username": "owner", "password": "hunter2"}
    ).json()["token"]
    return {"X-Auth-Token": token}


def test_book_scout_state_requires_auth(api_client) -> None:
    assert api_client.get("/api/browser/extensions/book-scout").status_code == 401


def test_book_scout_state_and_install_route(api_client) -> None:
    headers = _login(api_client)
    res = api_client.get("/api/browser/extensions/book-scout", headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["extension"]["status"] in ("not_configured", "failed", "installed")
    assert body["amo_url"].startswith("https://addons.mozilla.org/")
    assert body["engine"] == "camoufox"

    install = api_client.post(
        "/api/browser/extensions/book-scout/install-xpi",
        headers=headers,
        files={"file": ("scrivy.xpi", _xpi(), "application/x-xpinstall")},
    )
    assert install.status_code == 200, install.text
    assert install.json()["manifest"]["version"] == "0.1.2"
    after = api_client.get("/api/browser/extensions/book-scout", headers=headers).json()
    assert after["extension"]["version"] == "0.1.2"
    assert after["extension"]["status"] in ("installed", "validated")


def test_book_scout_live_verify_route_uses_the_real_browser(api_client) -> None:
    """The button the owner taps: opens a search, reports what was rendered."""
    headers = _login(api_client)
    page = _Page([_badge_payload()])
    browser = _Browser(page)

    async def launch(*_a: Any, **_k: Any) -> Any:
        return None

    browser.launch = launch  # type: ignore[attr-defined]
    api_client.app.state.browser_manager = browser

    res = api_client.post(
        "/api/browser/extensions/book-scout/verify",
        headers=headers, json={"query": "grief memoir", "wait_seconds": 5},
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["counts"]["with_bsr"] == 2
    assert data["sample"][0]["bsr"] == "#1,234"
    assert any(s["label"] == "Median BSR" for s in data["niche"])
    assert browser.closed == [page]
