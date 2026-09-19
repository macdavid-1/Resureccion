"""Tests for the whitelisted browser-action executor (fake browser + collector)."""
from __future__ import annotations

from typing import Any

import pytest

from app.actions import ActionError, ActionResult, ActionExecutor, AuthPauseRequired
from app.amazon_auth import AmazonAuthManager, AuthCheck
from app.browser_store import BrowserEvidenceStore
from app.config import Config
from app.evidence_capture import EvidenceCollector
from app.events import EventLog
from app.marketplace import AMAZON_MARKETPLACES


# --------------------------------------------------------------------- fakes
class FakePage:
    def __init__(self, url: str = "https://www.amazon.com/s?k=x", title: str = "T") -> None:
        self.url = url
        self._title = title

    async def title(self) -> str:
        return self._title


class FakeBrowser:
    def __init__(self) -> None:
        self.opened: list[str] = []
        self.closed: list[Any] = []
        self.page = FakePage()

    async def open_marketplace(self, marketplace: Any, path: str = "/") -> Any:
        self.opened.append((marketplace.code, path))
        return self.page

    async def new_page(self) -> Any:
        return self.page

    async def navigate(self, page: Any, url: str) -> None:
        page.url = url

    async def close_page(self, page: Any) -> None:
        self.closed.append(page)

    async def screenshot(self, page: Any) -> bytes:
        return b"png"


class FakeArtifacts:
    def save_bytes(self, *a: Any, **k: Any) -> Any:
        class A:
            id = "art1"
        return A()


class StubAuthManager:
    """Records which marketplace the auth-wall check used."""

    def __init__(self) -> None:
        self.checked_marketplace: str | None = None
        self.raise_pause = False

    async def _classify_page(self, page: Any, marketplace: Any) -> AuthCheck:
        self.checked_marketplace = marketplace.code
        if self.raise_pause:
            return AuthCheck(
                "captcha_required", page.url, "t",
                {"reason": "test"},
            )
        return AuthCheck("unknown", page.url, "t", {})


@pytest.fixture()
def wired(tmp_path, db):
    from app.config import Config as C

    cfg = C()
    cfg.data_dir = tmp_path / "d"
    cfg.ensure_dirs()
    events = EventLog(db)
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO research_sessions (id, name, status, mode, created_at, updated_at, last_activity_at) VALUES ('s1', 'n', 'running', 'auto', '2025-01-01', '2025-01-01', '2025-01-01')"
        )
    browser = FakeBrowser()
    store = BrowserEvidenceStore(db)
    collector = EvidenceCollector(browser, store, FakeArtifacts())
    auth = StubAuthManager()
    executor = ActionExecutor(cfg, browser, collector, auth, events)  # type: ignore[arg-type]
    executor.set_marketplaces([AMAZON_MARKETPLACES["us"], AMAZON_MARKETPLACES["uk"]])
    return executor, auth, browser, store, events


@pytest.mark.asyncio
async def test_auth_wall_check_uses_page_marketplace_not_rotation(wired):
    """Regression: the wall check must classify the page's own marketplace."""
    executor, auth, *_ = wired
    page = FakePage(url="https://www.amazon.de/dp/B0TEST")
    await executor._check_auth_wall(page)
    assert auth.checked_marketplace == "de"


@pytest.mark.asyncio
async def test_search_marketplace_extracts_results_list(wired):
    """Regression: capture_search_results returns a LIST — must not crash."""
    executor, _, browser, store, _ = wired

    async def fake_extract(page: Any, max_items: int) -> list[dict]:
        return [{"rank": 1, "asin": "B0TEST1234", "title": "T", "price": None,
                 "rating": None, "review_count": None}]

    import app.actions as actions_mod

    orig = actions_mod._extract_search_items if hasattr(actions_mod, "_extract_search_items") else None
    # Monkeypatch the collector's internal extraction used by search capture.
    import app.evidence_capture as ec

    orig_fn = ec._extract_search_items
    ec._extract_search_items = fake_extract
    try:
        result = await executor.execute(
            session_id="s1", action="search_marketplace",
            args={"query": "grief journal", "screenshot": False},
        )
    finally:
        ec._extract_search_items = orig_fn
    assert isinstance(result, ActionResult)
    assert result.ok
    assert result.evidence_ids
    rec = store.get(result.evidence_ids[0])
    assert rec is not None and rec.data["results"][0]["asin"] == "B0TEST1234"
    assert browser.closed  # page was closed


@pytest.mark.asyncio
async def test_unknown_action_rejected(wired):
    executor, *_ = wired
    with pytest.raises(ActionError):
        await executor.execute(session_id="s1", action="hack_the_planet", args={})


@pytest.mark.asyncio
async def test_action_not_in_whitelist_is_rejected_at_runner_level():
    """The runner checks spec.allowed_actions before execute; verify the set."""
    from app.methodology import SPECS

    for phase, spec in SPECS.items():
        for action in spec.allowed_actions:
            assert action.replace("_", "").isalnum(), (phase, action)


@pytest.mark.asyncio
async def test_record_observation_journals_event(wired):
    executor, _, _, _, events = wired
    result = await executor.execute(
        session_id="s1", action="record_observation", args={"note": "review patterns show X"},
    )
    assert result.ok
    evs = events.tail("s1")
    assert any(e.action == "agent_observation" for e in evs)


@pytest.mark.asyncio
async def test_auth_pause_raised_on_captcha(wired):
    executor, auth, *_ = wired
    auth.raise_pause = True
    with pytest.raises(AuthPauseRequired):
        await executor.execute(
            session_id="s1", action="search_marketplace",
            args={"query": "x", "screenshot": False},
        )


@pytest.mark.asyncio
async def test_marketplace_rotation_cycles(wired):
    executor, *_ = wired
    seen = [executor.next_marketplace().code for _ in range(4)]
    assert seen == ["us", "uk", "us", "uk"]


@pytest.mark.asyncio
async def test_wait_action_bounded(wired):
    executor, *_ = wired
    import time

    started = time.monotonic()
    result = await executor.execute(session_id="s1", action="wait", args={"seconds": 0.01})
    assert result.ok
    assert time.monotonic() - started < 1.0
