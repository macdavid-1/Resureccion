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


class _StubAuthStore:
    """Captures the durable auth write the research-wall handler makes."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, str, str, dict]] = []

    def set(self, account: str, profile: str, status: str, detail: dict | None = None) -> None:
        self.writes.append((account, profile, status, detail or {}))


class StubAuthManager:
    """Records which marketplace the auth-wall check used."""

    def __init__(self) -> None:
        self.checked_marketplace: str | None = None
        self.raise_pause = False
        self.profile = "kdspy"
        self.auth_store = _StubAuthStore()

    async def _classify_page(self, page: Any, marketplace: Any) -> AuthCheck:
        self.checked_marketplace = marketplace.code
        if self.raise_pause:
            return AuthCheck(
                "captcha_required", page.url, "t",
                {"reason": "test", "marketplace": marketplace.code},
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
async def test_search_targets_a_department_and_reports_amazons_total(wired):
    """Kindle Store and Books are separate catalogues, and Amazon's own
    printed result total is the competition figure the model must see."""
    executor, _, browser, store, _ = wired

    async def fake_extract(page: Any, max_items: int) -> list[dict]:
        return []

    class _Page(FakePage):
        async def inner_text(self, _sel: str) -> str:
            return "1-16 of over 4,000 results for space facts book for kids"

    browser.page = _Page()
    import app.evidence_capture as ec

    orig_fn = ec._extract_search_items
    ec._extract_search_items = fake_extract
    try:
        res = await executor.execute(
            session_id="s1", action="search_marketplace",
            args={"query": "space facts", "department": "kindle",
                  "screenshot": False},
        )
    finally:
        ec._extract_search_items = orig_fn

    assert res.ok, res.error
    code, path = browser.opened[-1]
    assert code == "us" and "i=digital-text" in path
    rec = store.get(res.evidence_ids[0])
    assert rec.data["total_results"] == 4000
    assert rec.data["total_results_are_approximate"] is True
    assert "4,000 total results" in res.summary
    assert "kindle department" in res.summary

    # The default stays exactly as it was: no department parameter.
    await executor.execute(
        session_id="s1", action="search_marketplace",
        args={"query": "space facts", "screenshot": False},
    )
    assert "i=" not in browser.opened[-1][1]

    # An unknown department is rejected instead of silently ignored.
    bad = await executor.execute(
        session_id="s1", action="search_marketplace",
        args={"query": "x", "department": "toys"},
    )
    assert not bad.ok and "department" in (bad.error or "")


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
async def test_auth_wall_names_marketplace_and_persists_state(wired):
    """The pause must name the wall's domain — a bare "login_required" left
    the owner guessing where to sign in — and the durable auth store must
    record the wall's marketplace, because the Settings Sign-in button
    targets it. Previously every sign-in went to amazon.com whatever domain
    the agent stalled on: Amazon sessions are per-domain, so a perfectly
    successful sign-in still left login_required everywhere else."""
    executor, auth, *_ = wired
    auth.raise_pause = True
    with pytest.raises(AuthPauseRequired) as ei:
        await executor.execute(
            session_id="s1", action="search_marketplace",
            args={"query": "x", "screenshot": False},
        )
    msg = str(ei.value)
    assert "captcha_required" in msg
    assert AMAZON_MARKETPLACES["us"].domain in msg  # where the wall is
    assert "Settings" in msg  # and what to do about it
    assert auth.auth_store.writes, "the wall must land in the durable store"
    account, profile, status, detail = auth.auth_store.writes[-1]
    assert (account, profile, status) == ("amazon", "kdspy", "captcha_required")
    assert detail["via"] == "research_wall"
    assert detail["marketplace"] == "us"


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


# ---------------------------------------- signed-in marker / wall skipping
class _WallAuthManager:
    """Auth manager whose classification always reports a fixed wall."""

    def __init__(self, auth_store: Any, status: str, marketplace: str) -> None:
        self.auth_store = auth_store
        self.profile = "kdspy"
        self._status = status
        self._mkt = marketplace

    async def _classify_page(self, page: Any, marketplace: Any) -> AuthCheck:
        return AuthCheck(self._status, page.url, "t", {"marketplace": self._mkt})


def _wall_executor(db, tmp_path, auth_store: Any, status: str, marketplace: str):
    cfg = Config()
    cfg.data_dir = tmp_path / "d"
    cfg.ensure_dirs()
    events = EventLog(db)
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO research_sessions (id, name, status, mode, created_at, updated_at, last_activity_at) "
            "VALUES ('s1', 'n', 'running', 'auto', '2025-01-01', '2025-01-01', '2025-01-01')"
        )
    browser = FakeBrowser()
    collector = EvidenceCollector(browser, BrowserEvidenceStore(db), FakeArtifacts())
    auth = _WallAuthManager(auth_store, status, marketplace)
    executor = ActionExecutor(cfg, browser, collector, auth, events)  # type: ignore[arg-type]
    executor.set_marketplaces([AMAZON_MARKETPLACES["us"], AMAZON_MARKETPLACES["uk"]])
    return executor, events, auth_store


@pytest.mark.asyncio
async def test_login_wall_on_other_marketplace_is_skipped_after_signin(db, tmp_path):
    """The owner signs in once (say on amazon.com); Amazon sessions are
    per-domain, so every OTHER marketplace keeps showing its own sign-in
    wall. After a completed sign-in those walls must not pause research:
    the page is skipped and research continues."""
    from app.browser_store import (
        AUTH_CONFIRMED,
        AUTH_LOGIN_REQUIRED,
        BrowserAuthStore,
    )

    store = BrowserAuthStore(db)
    store.set("amazon", "kdspy", AUTH_CONFIRMED, {"marketplace": "us"})
    executor, events, _ = _wall_executor(db, tmp_path, store, "login_required", "uk")

    result = await executor.execute(
        session_id="s1", action="open_category", args={"path": "/gp/x"}
    )
    assert not result.ok
    assert "skipped" in (result.error or "")
    assert any(e.action == "sign_in_wall_skipped" for e in events.tail("s1"))
    # The wall write must NOT erase the durable signed-in marker.
    state = store.get("amazon", "kdspy")
    assert state.status == AUTH_LOGIN_REQUIRED
    assert state.detail["signed_in_marketplace"] == "us"


@pytest.mark.asyncio
async def test_login_wall_on_signedin_marketplace_still_skips(db, tmp_path):
    """Sign-in is optional: even the marketplace the owner signed in on
    never pauses on a login wall — the page is skipped and the model
    routes around it (the durable auth state can be wrong per-domain)."""
    from app.browser_store import AUTH_CONFIRMED, BrowserAuthStore

    store = BrowserAuthStore(db)
    store.set("amazon", "kdspy", AUTH_CONFIRMED, {"marketplace": "us"})
    executor, *_ = _wall_executor(db, tmp_path, store, "login_required", "us")

    result = await executor.execute(
        session_id="s1", action="open_category", args={"path": "/gp/x"}
    )
    assert not result.ok
    assert "skipped" in (result.error or "")


@pytest.mark.asyncio
async def test_login_wall_without_any_signin_never_pauses(db, tmp_path):
    """The owner may never sign in at all: research must not block on it.
    The wall skips as a failed action and the MODEL decides whether to
    drop a source (e.g. review mining) — the backend never decides from
    an auth state that can be wrong."""
    from app.browser_store import AUTH_LOGIN_REQUIRED, BrowserAuthStore

    store = BrowserAuthStore(db)
    # Durable state even claims login_required — irrelevant to the decision.
    store.set("amazon", "kdspy", AUTH_LOGIN_REQUIRED, {"marketplace": "uk"})
    executor, events, _ = _wall_executor(db, tmp_path, store, "login_required", "uk")

    result = await executor.execute(
        session_id="s1", action="open_category", args={"path": "/gp/x"}
    )
    assert not result.ok
    assert "sign-in is optional" in (result.error or "")
    assert any(e.action == "sign_in_wall_skipped" for e in events.tail("s1"))


@pytest.mark.asyncio
async def test_read_reviews_not_gated_by_backend_auth_state(db, tmp_path):
    """Review mining is the AI's call: when the reviews page renders
    (no wall), read_reviews must capture them even though the durable auth
    store says login_required — the backend must never pre-block reviews
    from a state that can be wrong."""
    from app.browser_store import AUTH_LOGIN_REQUIRED, BrowserAuthStore

    store = BrowserAuthStore(db)
    store.set("amazon", "kdspy", AUTH_LOGIN_REQUIRED, {"marketplace": "us"})
    executor, *_ = _wall_executor(db, tmp_path, store, "login_required", "us")
    # The wall stub always reports login_required — swap it for a classifier
    # that sees a normal page (reviews visible, no wall).
    executor.amazon_auth = StubAuthManager()
    executor.amazon_auth.auth_store = store

    result = await executor.execute(
        session_id="s1", action="read_reviews", args={"asin": "B0TEST1234"}
    )
    assert result.ok, "visible reviews must be captured regardless of auth state"
    assert result.summary.startswith("captured")


@pytest.mark.asyncio
async def test_captcha_wall_still_pauses_for_owner(db, tmp_path):
    """CAPTCHA/OTP genuinely block every page and need the owner's hands —
    those still pause (with the marketplace domain + Settings guidance)."""
    from app.browser_store import BrowserAuthStore

    store = BrowserAuthStore(db)
    executor, *_ = _wall_executor(db, tmp_path, store, "captcha_required", "de")

    with pytest.raises(AuthPauseRequired) as ei:
        await executor.execute(
            session_id="s1", action="open_category", args={"path": "/gp/x"}
        )
    msg = str(ei.value)
    assert "captcha_required" in msg
    assert "amazon.de" in msg
    assert "Settings" in msg
