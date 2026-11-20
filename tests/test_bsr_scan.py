"""BSR on a search page WITHOUT the add-on.

Scrivy Book Scout's own trick is a same-origin `fetch('/dp/<asin>')` from the
search page, with the profile's cookies, then a regex for the printed rank.
That is not a Scrivy service, so Resurrección can do exactly the same in its
own browser and get BSR with nothing installed. These tests pin that path:

- the parse rules are the add-on's own (US + localized formats, category
  chain), and a robot wall is never read as "no rank";
- the scan fetches in-page (so it carries the session), in page order, and
  reports failures as failures;
- `book_scout_scan` uses the add-on when it is running and falls back to the
  product-page scan when it is not — one action either way.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.bsr_scan import (
    parse_localized_int,
    parse_product_bsr,
    result_asins,
    scan_page_bsr,
)

PRODUCT_US = """
<html><body>
<span id="productTitle">  The Grief Memoir  </span>
<div id="detailBullets_feature_div">
  <li><span class="a-list-item">Best Sellers Rank:
    <span>#4,231 in Books (See Top 100 in Books)</span>
    <span>#12 in Grief &amp; Loss (Books)</span>
    <span>#88 in Memoirs (Books)</span>
  </span></li>
</div>
<div id="publisher">Independently published (March 2025)</div>
</body></html>
"""

PRODUCT_DE = """
<html><body>
<span id="productTitle">Trauer</span>
Bestseller-Rang: Nr. 1.234 in B\u00fccher (Siehe Top 100 in B\u00fccher)
</body></html>
"""

ROBOT_WALL = """
<html><body><h4>Enter the characters you see below</h4>
<p>Sorry, we just need to make sure you're not a robot.</p>
<p>api-services-support@amazon.com</p></body></html>
"""

NO_RANK = "<html><body><span id=\"productTitle\">No rank here</span></body></html>"


def test_parse_localized_int_handles_every_separator():
    assert parse_localized_int("1,234") == 1234
    assert parse_localized_int("1.234") == 1234
    assert parse_localized_int("1 234") == 1234
    assert parse_localized_int("") is None


def test_parse_product_bsr_reads_the_rank_and_the_category_chain():
    parsed = parse_product_bsr(PRODUCT_US)
    assert parsed["rank"] == 4231
    assert parsed["category"] == "Books"
    assert parsed["title"] == "The Grief Memoir"
    assert parsed["indie"] is True
    assert {"rank": 12, "category": "Grief &amp; Loss"} in parsed["ranks"]
    assert {"rank": 88, "category": "Memoirs"} in parsed["ranks"]
    assert parsed["ranks"][0] == {"rank": 4231, "category": "Books"}
    assert parsed["blocked"] is False


def test_parse_product_bsr_handles_a_localized_storefront():
    parsed = parse_product_bsr(PRODUCT_DE)
    assert parsed["rank"] == 1234
    assert parsed["category"].startswith("B\u00fccher")


def test_a_robot_wall_is_flagged_not_read_as_an_unranked_book():
    parsed = parse_product_bsr(ROBOT_WALL)
    assert parsed["rank"] is None
    assert parsed["blocked"] is True


def test_a_page_without_a_printed_rank_is_not_blocked():
    parsed = parse_product_bsr(NO_RANK)
    assert parsed["rank"] is None and parsed["blocked"] is False


# --------------------------------------------------------------------- fakes
class FakePage:
    """Dispatches on the script the collector/action actually sends."""

    def __init__(
        self,
        *,
        asins: list[str] | None = None,
        html_by_asin: dict[str, str] | None = None,
        addon: bool = False,
        addon_payload: dict[str, Any] | None = None,
        status_by_asin: dict[str, int] | None = None,
    ) -> None:
        self.asins = asins or []
        self.html_by_asin = html_by_asin or {}
        self.addon = addon
        self.addon_payload = addon_payload
        self.status_by_asin = status_by_asin or {}
        self.fetched: list[str] = []
        self.url = "https://www.amazon.com/s?k=grief+memoir&i=stripbooks"

    async def title(self) -> str:
        return "Amazon.com : grief memoir"

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        if "!!document.querySelector" in script:
            return self.addon
        if "scrivy" in script:  # badge/niche extraction
            return self.addon_payload
        if "s-search-result" in script:
            return self.asins
        if "fetch(`/dp/" in script:
            asin = (arg or {}).get("asin")
            self.fetched.append(str(asin))
            status = self.status_by_asin.get(str(asin), 200)
            return {
                "status": status,
                "url": f"https://www.amazon.com/dp/{asin}",
                "html": self.html_by_asin.get(str(asin), "") if status == 200 else "",
            }
        return self.addon_payload


class FakeBrowser:
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


class Artifacts:
    def save_bytes(self, *a: Any, **k: Any) -> Any:
        return type("A", (), {"id": "art1"})()


class Auth:
    profile = "kdspy"
    auth_store = None

    async def _classify_page(self, page: Any, marketplace: Any) -> Any:
        from app.amazon_auth import AuthCheck

        return AuthCheck("unknown", page.url, "t", {})


# ---------------------------------------------------------------------- scan
@pytest.mark.asyncio
async def test_scan_fetches_each_result_page_in_order_with_counts():
    page = FakePage(
        asins=["B0AAA11111", "B0BBB22222", "B0CCC33333"],
        html_by_asin={
            "B0AAA11111": PRODUCT_US,
            "B0BBB22222": NO_RANK,
            "B0CCC33333": ROBOT_WALL,
        },
    )
    result = await scan_page_bsr(page, max_items=10, concurrency=2, pacing=(0, 0))
    assert result["scanned"] == 3
    assert result["with_bsr"] == 1
    assert result["failed"] == 2
    assert result["blocked"] == 1
    assert [r["asin"] for r in result["rows"]] == page.asins
    assert list(dict.fromkeys(page.fetched)) == page.asins
    # A wall after real ranks were already read is final — no retry storm.
    assert page.fetched.count("B0CCC33333") == 1
    good = result["rows"][0]
    assert good["rank"] == 4231 and good["category"] == "Books"
    assert result["rows"][1]["error"] == "no rank printed"
    assert result["rows"][2]["error"] == "robot check"


@pytest.mark.asyncio
async def test_scan_reports_a_failed_fetch_rather_than_a_missing_rank():
    page = FakePage(
        asins=["B0AAA11111"], html_by_asin={}, status_by_asin={"B0AAA11111": 503}
    )
    result = await scan_page_bsr(page, pacing=(0, 0))
    assert result["with_bsr"] == 0
    assert result["rows"][0]["error"] == "HTTP 503"


@pytest.mark.asyncio
async def test_a_wall_streak_stops_the_scan_instead_of_hammering_amazon():
    asins = [f"B0AAA{i:05d}" for i in range(8)]
    page = FakePage(asins=asins, html_by_asin={a: ROBOT_WALL for a in asins})
    result = await scan_page_bsr(page, pacing=(0, 0))
    assert result["with_bsr"] == 0
    # Four walls in a row is enough to stop (in-flight workers can finish one
    # more), so most of the page is skipped instead of hammered.
    assert 4 <= result["blocked"] <= 6
    assert result["skipped"] >= 2
    assert result["rows"][-1]["skipped"].startswith("rate limited")
    assert len(page.fetched) <= 6


@pytest.mark.asyncio
async def test_result_asins_dedupes_and_caps():
    page = FakePage(
        asins=["B0AAA11111", "B0AAA11111", "", "B0BBB22222", "B0CCC33333"]
    )
    assert await result_asins(page, max_items=2) == ["B0AAA11111", "B0BBB22222"]


# -------------------------------------------------------------------- action
@pytest.mark.asyncio
async def test_the_same_action_falls_back_to_product_pages_without_the_addon(db):
    from app.actions import ActionExecutor
    from app.browser_store import BrowserEvidenceStore
    from app.events import EventLog
    from app.evidence_capture import EvidenceCollector
    from app.marketplace import AMAZON_MARKETPLACES

    with db.tx() as conn:
        conn.execute(
            "INSERT INTO research_sessions (id, name, status, mode, created_at, updated_at,"
            " last_activity_at) VALUES ('s1', 'n', 'running', 'auto', '2025-01-01',"
            " '2025-01-01', '2025-01-01')"
        )
    page = FakePage(
        asins=["B0AAA11111", "B0BBB22222"],
        html_by_asin={"B0AAA11111": PRODUCT_US, "B0BBB22222": PRODUCT_US},
        addon=False,
    )
    browser = FakeBrowser(page)
    store = BrowserEvidenceStore(db)
    executor = ActionExecutor(
        type("Cfg", (), {})(), browser,  # type: ignore[arg-type]
        EvidenceCollector(browser, store, Artifacts()), Auth(), EventLog(db),
    )
    executor.set_marketplaces([AMAZON_MARKETPLACES["us"]])

    result = await executor.execute(
        session_id="s1", action="book_scout_scan",
        args={"query": "grief memoir", "screenshot": False},
    )
    assert result.ok, result.error
    assert "add-on not loaded" in result.summary
    assert "2/2 results carry a BSR" in result.summary
    rec = store.get(result.evidence_ids[0])
    assert rec.kind == "bsr_scan"
    assert rec.data["source"] == "product_pages"
    assert rec.data["rows"][0]["rank"] == 4231
    # The search page was the Books catalogue, and it was closed afterwards.
    assert browser.opened[-1][1].endswith("i=stripbooks")
    assert browser.closed == [page]


@pytest.mark.asyncio
async def test_the_action_still_prefers_the_addon_when_it_is_running(db):
    from app.actions import ActionExecutor
    from app.browser_store import BrowserEvidenceStore
    from app.events import EventLog
    from app.evidence_capture import EvidenceCollector
    from app.marketplace import AMAZON_MARKETPLACES

    with db.tx() as conn:
        conn.execute(
            "INSERT INTO research_sessions (id, name, status, mode, created_at, updated_at,"
            " last_activity_at) VALUES ('s1', 'n', 'running', 'auto', '2025-01-01',"
            " '2025-01-01', '2025-01-01')"
        )
    payload = {
        "url": "https://www.amazon.com/s?k=grief+memoir&i=stripbooks",
        "query": "grief memoir",
        "badges": [{
            "asin": "B0AAA11111", "text": "Scrivy BSR #1,234 Est. sales ~120/mo",
            "title": "The Grief Memoir",
            "stats": [{"label": "BSR", "value": "#1,234"}], "indie": False,
            "unavailable": False,
        }],
        "niche": {"text": "x", "stats": [{"label": "Median BSR", "value": "#1,234"}], "rows": []},
    }
    page = FakePage(addon=True, addon_payload=payload)
    browser = FakeBrowser(page)
    store = BrowserEvidenceStore(db)
    executor = ActionExecutor(
        type("Cfg", (), {})(), browser,  # type: ignore[arg-type]
        EvidenceCollector(browser, store, Artifacts()), Auth(), EventLog(db),
    )
    executor.set_marketplaces([AMAZON_MARKETPLACES["us"]])
    result = await executor.execute(
        session_id="s1", action="book_scout_scan",
        args={"query": "grief memoir", "screenshot": False, "wait_seconds": 1},
    )
    assert result.ok, result.error
    assert "Book Scout:" in result.summary
    rec = store.get(result.evidence_ids[0])
    assert rec.kind == "book_scout_scan"
    assert page.fetched == []  # no per-book fetches when the add-on answered


def test_the_native_scan_is_an_observation_not_an_estimate():
    from app.claims import is_estimate_kind

    assert is_estimate_kind("bsr_scan") is False


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


def test_verify_route_reports_the_native_source_when_no_addon(api_client) -> None:
    headers = _login(api_client)
    page = FakePage(
        asins=["B0AAA11111"],
        html_by_asin={"B0AAA11111": PRODUCT_US},
        addon=False,
    )
    browser = FakeBrowser(page)

    async def launch(*_a: Any, **_k: Any) -> Any:
        return None

    browser.launch = launch  # type: ignore[attr-defined]
    browser.shutdown = launch  # type: ignore[attr-defined]
    api_client.app.state.browser_manager = browser

    res = api_client.post(
        "/api/browser/extensions/book-scout/verify",
        headers=headers, json={"query": "grief memoir", "wait_seconds": 5},
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["source"] == "product_pages"
    assert data["counts"]["with_bsr"] == 1
    assert data["sample"][0]["bsr"] == "#4,231"
    assert data["sample"][0]["category"] == "Books"
    assert "product page" in data["note"]
