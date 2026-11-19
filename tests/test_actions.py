"""Tests for the whitelisted browser-action executor (fake browser + collector)."""
from __future__ import annotations

from typing import Any

import pytest

from app.actions import ActionError, ActionExecutor, ActionResult, AuthPauseRequired
from app.amazon_auth import AuthCheck
from app.browser_store import BrowserEvidenceStore
from app.config import Config
from app.events import EventLog
from app.evidence_capture import EvidenceCollector
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

    actions_mod._extract_search_items if hasattr(actions_mod, "_extract_search_items") else None
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


class _FakeLocator:
    """A locator that records the click it was asked to perform."""

    def __init__(self, page: _VerbPage, matched: bool) -> None:
        self.page = page
        self.matched = matched
        self.first = self

    async def count(self) -> int:
        return 1 if self.matched else 0

    async def scroll_into_view_if_needed(self, timeout: int = 0) -> None:
        return None

    async def bounding_box(self) -> dict[str, float]:
        return {"x": 10.0, "y": 20.0, "width": 100.0, "height": 30.0}

    async def click(self, timeout: int = 0) -> None:
        self.page.clicked.append("locator")

    async def fill(self, _value: str) -> None:
        self.page.filled.append(_value)

    async def hover(self, timeout: int = 0) -> None:
        self.page.hovered.append("locator")

    async def select_option(self, value: Any = None, **kw: Any) -> None:
        self.page.selected.append(str(value if value is not None else kw.get("label")))

    async def wait_for(self, timeout: int = 0) -> None:
        self.page.waited.append("locator")


class _FakeMouse:
    def __init__(self, page: _VerbPage) -> None:
        self.page = page

    async def move(self, *a: Any, **k: Any) -> None:
        self.page.moves += 1

    async def down(self) -> None:
        self.page.clicked.append("down")

    async def up(self) -> None:
        self.page.clicked.append("up")

    async def wheel(self, _x: int, dy: int) -> None:
        self.page.wheels.append(dy)


class _FakeKeyboard:
    def __init__(self, page: _VerbPage) -> None:
        self.page = page

    async def type(self, text: str, delay: int = 0) -> None:
        self.page.typed.append((text, delay))

    async def press(self, key: str) -> None:
        self.page.pressed.append(key)


class _VerbPage:
    """Playwright-shaped page for the model's own browser verbs."""

    def __init__(self, url: str = "https://www.amazon.com/s?k=space+facts") -> None:
        self.url = url
        self.clicked: list[str] = []
        self.moves = 0
        self.wheels: list[int] = []
        self.typed: list[tuple[str, int]] = []
        self.pressed: list[str] = []
        self.filled: list[str] = []
        self.goto_urls: list[str] = []
        self.hovered: list[str] = []
        self.selected: list[str] = []
        self.waited: list[str] = []
        self.js: list[str] = []
        self.history: list[str] = []
        self.mouse = _FakeMouse(self)
        self.keyboard = _FakeKeyboard(self)

    async def evaluate(self, js: str, *args: Any) -> Any:
        self.js.append(js)
        return {"printed_total": 30000, "rows": 16}

    async def wait_for_selector(self, selector: str, timeout: int = 0) -> None:
        self.waited.append(selector)

    async def go_back(self, wait_until: str = "", timeout: int = 0) -> None:
        self.history.append("back")

    async def go_forward(self, wait_until: str = "", timeout: int = 0) -> None:
        self.history.append("forward")

    async def reload(self, wait_until: str = "", timeout: int = 0) -> None:
        self.history.append("reload")

    def is_closed(self) -> bool:
        return False

    async def goto(self, url: str, wait_until: str = "", timeout: int = 0) -> None:
        self.goto_urls.append(url)
        self.url = url

    async def wait_for_load_state(self, *a: Any, **k: Any) -> None:
        return None

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self, matched=selector.lstrip("#.") != "")

    def get_by_text(self, text: str, exact: bool = False) -> _FakeLocator:
        return _FakeLocator(self, matched=text.strip().lower() == "dismiss")

    def get_by_role(self, role: str, name: str = "") -> _FakeLocator:
        return _FakeLocator(self, matched=False)


@pytest.mark.asyncio
async def test_dismiss_overlay_is_gone_the_model_clicks_it_itself(wired, monkeypatch):
    """The old 'dismiss_overlay' verb is REMOVED: it never did anything the
    model could observe. The model now clicks the refusal control itself with
    its own Playwright click, and that click is real."""
    monkeypatch.setenv("BROWSER_ACTION_PACE", "off")
    executor, *_ = wired
    page = _VerbPage()
    executor.browser.new_page = lambda: _async(page)  # type: ignore[assignment]

    with pytest.raises(ActionError):
        await executor.execute(session_id="s1", action="dismiss_overlay", args={})

    res = await executor.execute(
        session_id="s1", action="click", args={"target": "Dismiss"},
    )
    assert res.ok, res.error
    assert "Dismiss" in res.summary
    assert page.clicked, "nothing was clicked on the page"


@pytest.mark.asyncio
async def test_model_navigation_is_unrestricted_and_paced(wired, monkeypatch):
    """The model navigates anywhere it judges useful — Amazon, a competitor's
    own site, a forum. There is no host allow-list; the researcher decides
    where the evidence is (pacing is still applied)."""
    monkeypatch.setenv("BROWSER_ACTION_PACE", "off")
    executor, *_ = wired
    page = _VerbPage()
    executor.browser.new_page = lambda: _async(page)  # type: ignore[assignment]

    ok = await executor.execute(
        session_id="s1", action="navigate",
        args={"url": "https://www.amazon.co.uk/s?k=grief+journals"},
    )
    assert ok.ok, ok.error
    assert page.goto_urls == ["https://www.amazon.co.uk/s?k=grief+journals"]

    elsewhere = await executor.execute(
        session_id="s1", action="navigate", args={"url": "https://example.com/x"},
    )
    assert elsewhere.ok, elsewhere.error
    assert page.goto_urls[-1] == "https://example.com/x"


async def _async(page: Any) -> Any:
    return page


@pytest.mark.asyncio
async def test_full_playwright_surface_is_reachable(wired, monkeypatch):
    """Every verb a person has is available to the model — including the ones
    that used to be missing (scroll, hover, select, JS, history, reload) and
    the generic 'browser' escape hatch."""
    monkeypatch.setenv("BROWSER_ACTION_PACE", "off")
    executor, *_ = wired
    page = _VerbPage()
    executor.browser.new_page = lambda: _async(page)  # type: ignore[assignment]

    scrolled = await executor.execute(
        session_id="s1", action="scroll", args={"direction": "down", "amount": 2}
    )
    assert scrolled.ok and page.wheels, "a scroll request never moved the page"

    hovered = await executor.execute(
        session_id="s1", action="hover", args={"target": "Dismiss"}
    )
    assert hovered.ok, hovered.error

    selected = await executor.execute(
        session_id="s1", action="select_option",
        args={"target": "#sort", "value": "price-asc-rank"},
    )
    assert selected.ok and page.selected == ["price-asc-rank"], selected.error

    ran = await executor.execute(
        session_id="s1", action="evaluate",
        args={"js": "() => document.body.innerText.match(/of ([\\d,]+) results/)[1]"},
    )
    assert ran.ok, ran.error
    assert "30000" in ran.summary, "the JS value never reached the model"

    back = await executor.execute(session_id="s1", action="go_back", args={})
    assert back.ok and "back" in page.history
    forward = await executor.execute(session_id="s1", action="go_forward", args={})
    assert forward.ok and "forward" in page.history
    reloaded = await executor.execute(session_id="s1", action="reload", args={})
    assert reloaded.ok and "reload" in page.history

    refreshed = await executor.execute(session_id="s1", action="screenshot", args={})
    assert refreshed.ok, refreshed.error

    # The generic escape hatch runs the same verbs by name.
    generic = await executor.execute(
        session_id="s1", action="browser",
        args={"op": "scroll", "direction": "up"},
    )
    assert generic.ok, generic.error
    assert len(page.wheels) >= 2

    waited = await executor.execute(
        session_id="s1", action="wait_for", args={"target": "#s-result-info-bar"}
    )
    assert waited.ok, waited.error
    assert page.waited


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


class _ReviewEl:
    def __init__(self, text: str) -> None:
        self._text = text

    async def inner_text(self) -> str:
        return self._text

    async def get_attribute(self, _name: str) -> str | None:
        return None

    async def click(self, **_kwargs) -> None:
        return None


class _ReviewCard:
    def __init__(self, rating: str, title: str, body: str) -> None:
        self._parts = {
            "review-star-rating": _ReviewEl(rating),
            "review-title": _ReviewEl(title),
            "review-date": _ReviewEl("Reviewed in the United States on May 1, 2026"),
            "review-body": _ReviewEl(body),
            "helpful-vote-statement": _ReviewEl("3 people found this helpful"),
            "a-profile-name": _ReviewEl("A reader"),
        }

    async def query_selector(self, sel: str) -> Any:
        if "avp-badge" in sel:
            return _ReviewEl("Verified Purchase")
        for key, el in self._parts.items():
            if key in sel:
                return el
        return None

    async def inner_text(self) -> str:
        return ""


class ReviewsPage(FakePage):
    """A product page that renders its reviews once it has been scrolled."""

    def __init__(self) -> None:
        super().__init__(url="https://www.amazon.com/dp/B0TEST1234", title="Grief Journal")
        self.cards = [
            _ReviewCard("5.0 out of 5 stars", "Helped me", "This journal helped me write again."),
            _ReviewCard("1.0 out of 5 stars", "Thin", "Only forty pages and repeats itself."),
        ]
        self.mounted = False

    async def evaluate(self, script: str, *_args: Any) -> Any:
        if "scrollBy" in script:
            self.mounted = True
            return None
        if "querySelector" in script:
            return self.mounted
        return None

    async def wait_for_timeout(self, _ms: int) -> None:
        return None

    async def query_selector(self, sel: str) -> Any:
        if "total-review-count" in sel or "acrCustomerReviewText" in sel:
            return _ReviewEl("842 global ratings")
        return None

    async def query_selector_all(self, sel: str) -> list:
        if "data-hook='review'" in sel or sel.startswith("#customerReviews"):
            return self.cards if self.mounted else []
        return []

    async def inner_text(self, _sel: str) -> str:
        return ""


class _SuggestPage(FakePage):
    """Answers the completion call with a mix of store roots and real phrases."""

    async def evaluate(self, _js: str, *_args: Any) -> list[str]:
        return [
            "grief books",
            "grief journal",
            "sudden loss grief for widowers in year one",
            "books for women",
            "grief after a parent's stroke",
        ]


@pytest.mark.asyncio
async def test_autocomplete_refuses_a_generic_seed(db, tmp_path):
    """A generic seed only returns store roots, so it is refused before the
    request is made — with instructions the model can act on."""
    from app.browser_store import BrowserAuthStore

    store = BrowserAuthStore(db)
    executor, *_ = _wall_executor(db, tmp_path, store, "unknown", "us")
    result = await executor.execute(
        session_id="s1", action="autocomplete", args={"query": "grief"}
    )
    assert not result.ok
    assert "targeted phrase" in (result.error or "")
    assert "single word" in (result.error or "")
    assert executor.browser.opened == [], "a refused seed must not touch Amazon"


@pytest.mark.asyncio
async def test_autocomplete_hands_over_every_suggestion_and_marks_the_roots(db, tmp_path):
    """Which suggestions are worth pursuing is the model's judgement, not a
    word list's: nothing is hidden. The store roots are MARKED so the model can
    skip them knowingly."""
    from app.browser_store import BrowserAuthStore

    store = BrowserAuthStore(db)
    executor, *_ = _wall_executor(db, tmp_path, store, "unknown", "us")
    executor.browser.page = _SuggestPage(url="https://www.amazon.com/")

    result = await executor.execute(
        session_id="s1", action="autocomplete",
        args={"query": "sudden loss grief for widowers"},
    )
    assert result.ok, result.error
    assert result.summary.startswith("5 suggestions")
    assert "3 of them are store roots" in result.summary
    row = executor.collector.evidence_store.list("s1")[0]
    assert row.kind == "autocomplete"
    # Everything Amazon returned, in order, unfiltered.
    assert row.data["suggestions"] == [
        "grief books",
        "grief journal",
        "sudden loss grief for widowers in year one",
        "books for women",
        "grief after a parent's stroke",
    ]
    assert row.data["store_root_suggestions"] == [
        "grief books", "grief journal", "books for women",
    ]
    assert "you decide" in row.data["note"]


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
    executor.browser.page = ReviewsPage()

    result = await executor.execute(
        session_id="s1", action="read_reviews", args={"asin": "B0TEST1234"}
    )
    assert result.ok, "visible reviews must be captured regardless of auth state"
    assert result.summary.startswith("captured 2 review(s)")
    assert "1 critical" in result.summary
    # Stored as review_research — the kind the report dossier and the
    # follow-up record read (the old action wrote a kind nothing consumed).
    rows = executor.collector.evidence_store.list("s1")
    review_rows = [r for r in rows if r.kind == "review_research"]
    assert len(review_rows) == 1
    data = review_rows[0].data
    assert data["reviews_captured"] == 2
    assert data["reviews"][0]["text"] == "This journal helped me write again."
    assert data["reviews"][1]["rating"] == 1.0
    assert review_rows[0].screenshot_artifact_id == "art1"


@pytest.mark.asyncio
async def test_read_reviews_reports_why_a_page_had_nothing_to_read(db, tmp_path):
    """A page that renders no reviews must say so — an empty capture is not
    the same as "this book has no complaints", and the model has to know
    which it got."""
    from app.browser_store import BrowserAuthStore

    store = BrowserAuthStore(db)
    executor, *_ = _wall_executor(db, tmp_path, store, "unknown", "us")
    executor.amazon_auth = StubAuthManager()
    executor.amazon_auth.auth_store = store

    result = await executor.execute(
        session_id="s1", action="read_reviews", args={"asin": "B0NONE0001"}
    )
    assert result.ok
    assert result.summary.startswith("no reviews captured")
    assert "no review container matched" in result.summary


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
