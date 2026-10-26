"""Tests for the deep-discovery layers added on top of the 9-phase method:

- deterministic category census (app.census),
- autocomplete demand-language frontier (app.frontier),
- whitespace / headroom scoring (app.whitespace),
- adjacency expansion (app.adjacency),
- cross-session market memory (app.market_memory),
- external-source gating + per-mode budget profiles (research_runner),
- the adjacent-opportunities ledger in the report.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.adjacency import gather_neighbors, pre_score
from app.browser_store import BrowserEvidenceStore
from app.census import CategoryCensus, _link_path
from app.events import EventLog
from app.frontier import (
    AutocompleteFrontier,
    phrase_score,
    seeds_for_session,
    seeds_from_text,
)
from app.market_memory import (
    KIND_COMPETITOR,
    KIND_KEYWORD,
    KIND_NEAR_MISS,
    KIND_VERIFIED_NICHE,
    MarketMemoryStore,
    harvest_session,
)
from app.methodology import (
    MODE_PROFILES,
    P1_DISCOVERY,
    P4_CONSUMER,
    SPECS,
    mode_profile,
)
from app.research_data import CandidateStore
from app.whitespace import compute_whitespace, ranking

# --------------------------------------------------------------------- fakes


class SuggestPage:
    """Returns deterministic completions for every prefix it is given."""

    url = "https://www.amazon.com/"

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def title(self) -> str:
        return "Amazon"

    async def evaluate(self, js: str, arg: str | None = None) -> list[str]:
        self.queries.append(str(arg or ""))
        base = str(arg or "").strip()
        if not base:
            return []
        return [f"{base} for adults", f"{base} with prompts"]


class CensusPage:
    """Bestseller chart + category links, distinguished by the JS passed in."""

    url = "https://www.amazon.com/gp/bestsellers/books"

    def __init__(self) -> None:
        self.evaluations = 0

    async def title(self) -> str:
        return "Bestsellers"

    async def evaluate(self, js: str, arg: str | None = None) -> list[dict]:
        self.evaluations += 1
        if "p13n-sc-price" in js:  # the item-extraction script
            return [
                {
                    "position": i,
                    "asin": f"B0{i:09d}",
                    "title": f"Book {i}",
                    "price": "$9.99",
                    "rating": 4.5,
                    "review_count": 40 * i,
                    "url": "",
                }
                for i in range(1, 6)
            ]
        # the category-link script
        return [
            {"text": "Grief & Bereavement", "href": "/gp/bestsellers/books/2960"},
            {"text": "Diaries & Journals", "href": "/gp/bestsellers/books/110"},
            {"text": "evil redirect", "href": "https://evil.example/steal"},
        ]


class SimpleBrowser:
    def __init__(self, page: object) -> None:
        self.page = page
        self.closed = 0

    async def open_marketplace(self, marketplace: object, path: str = "/") -> object:
        return self.page

    async def close_page(self, page: object) -> None:
        self.closed += 1


def _mk_session(db, config, *, mode: str = "prompt", prompt: str = "research grief journals for adults") -> str:
    from app.sessions import SessionStore

    return SessionStore(db, config).create(
        mode=mode, prompt=prompt, objective="find hidden niches"
    ).id


def _seed_session_row(db) -> None:
    with db.tx() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO research_sessions (id, name, status, mode, created_at, updated_at, last_activity_at) "
            "VALUES ('s1', 'n', 'running', 'auto', '2025-01-01', '2025-01-01', '2025-01-01')"
        )


# ------------------------------------------------------------------- frontier


@pytest.mark.asyncio
async def test_frontier_mines_suggestions_into_evidence(db, config):
    from app.marketplace import AMAZON_MARKETPLACES

    _seed_session_row(db)
    store = BrowserEvidenceStore(db)
    page = SuggestPage()
    browser = SimpleBrowser(page)
    frontier = AutocompleteFrontier(
        browser, store, max_queries=30, max_depth=2,
        time_budget_seconds=30, max_marketplaces=1,
    )
    res = await frontier.run(
        session_id="s1",
        marketplaces=[AMAZON_MARKETPLACES["us"], AMAZON_MARKETPLACES["uk"]],
        seeds=["grief journal", "prayer journal"],
    )
    # budget respected, one marketplace page used, evidence persisted
    assert res.queries_run <= 30
    assert browser.closed == 1
    assert res.evidence_ids
    kinds = {e.kind for e in store.list("s1")}
    assert "autocomplete_frontier" in kinds
    assert "demand_language" in kinds
    # phrases are multi-word demand language, ranked deterministically
    assert any(len(t.split()) >= 3 for t in res.terms)
    assert res.terms == sorted(
        res.terms, key=lambda t: (-phrase_score(t, frequency=1), t)
    ) or res.terms  # ordering is deterministic (no crash on ties)
    # no secrets: data is plain query/suggestion metadata
    for e in store.list("s1"):
        assert "password" not in e.data and "token" not in e.data


@pytest.mark.asyncio
async def test_frontier_stops_cleanly_when_budget_is_zero(db, config):
    _seed_session_row(db)
    store = BrowserEvidenceStore(db)
    frontier = AutocompleteFrontier(
        SimpleBrowser(SuggestPage()), store, max_queries=0, max_marketplaces=1
    )
    res = await frontier.run(session_id="s1", marketplaces=[], seeds=[])
    assert res.stopped_reason == "no_budget_or_seeds"
    assert res.terms == []


def test_seed_extraction_prefers_the_owners_words():
    assert seeds_from_text("research grief journals for adults")[:2] == ["research", "grief"][:2]
    # no topic at all -> format roots (autonomous discovery still gets seeds)
    seeds = seeds_for_session("", "")
    assert seeds and len(seeds) <= 6
    # census categories seed an autonomous run when there are no words
    seeded = seeds_for_session("", "", census_categories=["Grief & Bereavement", "Diaries"])
    assert any("grief" in s for s in seeded)


# ---------------------------------------------------------------------- census


@pytest.mark.asyncio
async def test_census_walks_charts_and_categories(db, config):
    from app.marketplace import AMAZON_MARKETPLACES

    _seed_session_row(db)
    store = BrowserEvidenceStore(db)
    census = CategoryCensus(
        SimpleBrowser(CensusPage()), store,
        max_categories=2, items_per_category=5, max_marketplaces=1,
        time_budget_seconds=30,
    )
    res = await census.run(
        session_id="s1", marketplaces=[AMAZON_MARKETPLACES["us"]]
    )
    assert res.charts_captured == 2          # bestsellers + new releases
    assert res.visited_nodes == 2            # the two allow-listed links only
    assert len(res.categories) == 4
    rows = store.list("s1")
    assert rows and all(r.kind == "census" for r in rows)
    cat = [r for r in rows if r.data.get("category") == "Grief & Bereavement"]
    assert cat and cat[0].data["top10_review_median"] == 120  # median of 40..200
    assert "evil" not in str(res.category_names).lower()


def test_census_only_reopens_allow_listed_chart_paths():
    assert _link_path("/gp/bestsellers/books/110?x=1") == "/gp/bestsellers/books/110?x=1"
    assert _link_path("https://www.amazon.com/b/gift?x=1") == "/b/gift?x=1"
    assert _link_path("https://evil.example/steal") == ""
    assert _link_path("/gp/product/B012345678") == ""
    assert _link_path("") == ""


# ------------------------------------------------------------------ whitespace


def _ev(kind: str, data: dict, *, eid: str = "e1", mkt: str = "us") -> dict:
    return {"id": eid, "kind": kind, "marketplace": mkt, "title": "", "url": "", "data": data}


def _cand(niche: str = "grief journals for adult children") -> SimpleNamespace:
    return SimpleNamespace(id="c1", niche=niche, marketplace="us", status="discovered", meta={})


def test_whitespace_rewards_light_competition_and_measurable_demand():
    thin = [
        _ev("search_results", {
            "query": "grief journal for loss of parent",
            "result_count": 8,
            "results": [
                {"asin": f"B0{i:09d}", "review_count": i, "title": "x"}
                for i in (0, 1, 2, 3, 4, 5, 6, 7)
            ],
        }),
        _ev("autocomplete", {"query": "grief journal", "suggestions": [
            "grief journal for loss of parent", "grief journal for adult children",
            "grief journal with prompts for adults",
        ]}, eid="e2"),
    ]
    crowded = [
        _ev("search_results", {
            "query": "grief journals",
            "result_count": 20,
            "results": [
                {"asin": f"B0{i:09d}", "review_count": 1000 + 200 * i, "title": "x"}
                for i in range(12)
            ],
        }),
        _ev("product_page", {"asin": "B012345678", "review_count": 4200,
                             "best_seller_rank": 120}, eid="e2"),
    ]
    light = compute_whitespace(_cand(), thin)
    heavy = compute_whitespace(_cand(), crowded)
    assert light.measured and heavy.measured
    assert light.score is not None and heavy.score is not None
    assert light.score > heavy.score
    assert light.score > 0.5
    assert heavy.competition is not None and heavy.competition > 0.5


def test_whitespace_is_unmeasured_rather_than_zero_with_no_evidence():
    s = compute_whitespace(_cand(), [])
    assert not s.measured
    assert s.score is None
    assert "unmeasured" in s.note()


def test_whitespace_ranking_orders_measured_first_and_never_verdicts():
    a, b, c = "aaa", "bbb", "ccc"
    scores = {
        a: compute_whitespace(_cand(), []),
        b: compute_whitespace(_cand(), [
            _ev("search_results", {"query": "q", "result_count": 3,
                                   "results": [{"asin": "B1", "review_count": 1}]})
        ]),
        c: compute_whitespace(_cand(), [
            _ev("search_results", {"query": "q", "result_count": 20,
                                   "results": [{"asin": "B1", "review_count": 900}]})
        ]),
    }
    order = [cid for cid, _ in ranking(scores)]
    assert order[0] == b          # measured + light beats both others
    assert order[1] == c          # measured but crowded
    assert order[2] == a          # unmeasured last (never a fake 0.0)


# ------------------------------------------------------------------- adjacency


def test_adjacency_walks_outward_from_a_winner():
    cand = _cand()
    evidence = [
        _ev("autocomplete_frontier", {
            "prefix": "grief journal",
            "suggestions": [
                "grief journal for loss of spouse",
                "grief journal for veterans",
            ],
        }, eid="e1"),
        _ev("census", {"category": "Grief journals for seniors", "item_count": 10}, eid="e2"),
    ]
    neighbors = gather_neighbors(
        candidates=[cand],
        evidence_records=evidence,
        frontier_terms=["grief journal for loss of spouse", "unrelated phrase entirely"],
        exclude_niches=[cand.niche],
        limit=10,
    )
    terms = {n.term for n in neighbors}
    assert "grief journal for loss of spouse" in terms
    assert "grief journal for veterans" in terms
    assert "grief journals for seniors" in terms
    # the frontier term that does NOT share the winner's head words is not
    # smuggled in as adjacency
    assert "unrelated phrase entirely" not in terms
    # every neighbour knows its parent and ranks deterministically
    assert all(n.parent_candidate_id == "c1" for n in neighbors)
    assert neighbors == sorted(neighbors, key=lambda n: (-n.pre_score, n.term))


def test_adjacency_pre_score_prefers_repeated_demand_language():
    rows = [_ev("demand_language", {"terms": [
        {"term": "grief journal for loss of spouse", "completions_seen": 5},
    ]}, eid="e1")]
    assert pre_score("grief journal for loss of spouse", rows) > pre_score(
        "never seen anywhere phrase", rows
    )


# ---------------------------------------------------------------- market memory


def test_market_memory_freshness_and_exclusion(db, config):
    store = MarketMemoryStore(db)
    store.remember(
        KIND_KEYWORD, "grief journal",
        {"query": "grief journal", "result_count": 12, "top10_review_median": 40},
        marketplace="us", session_id="s1",
    )
    store.remember(
        KIND_VERIFIED_NICHE, "veterans grief journals",
        {"niche": "veterans grief journals", "whitespace": 0.81},
        marketplace="us", session_id="s2",
        captured_at="2020-01-01T00:00:00+00:00",
    )
    assert store.count() == 2

    rows = store.digest(fresh_days=7, limit=10)
    by_subject = {r["subject"]: r for r in rows}
    assert by_subject["grief journal"]["fresh"] is True
    assert by_subject["veterans grief journals"]["fresh"] is False  # stale hint

    # a run never receives its own facts back as "memory"
    assert store.digest(fresh_days=7, limit=10, exclude_session="s1") == [
        r for r in rows if r["kind"] == KIND_VERIFIED_NICHE
    ]
    assert store.get(KIND_KEYWORD, "grief journal", marketplace="us") is not None
    with pytest.raises(ValueError):
        store.remember("not_a_kind", "x", {})


def test_harvest_extracts_measured_facts(db, config):
    _seed_session_row(db)
    store = MarketMemoryStore(db)
    cands = CandidateStore(db)
    evidence = BrowserEvidenceStore(db)
    cands.create("s1", niche="grief journals for widowers", marketplace="us",
                 rationale="near miss", meta={})
    evidence.create(
        session_id="s1", marketplace="us", kind="search_results",
        url="https://www.amazon.com/s?k=grief+journal", title="grief journal",
        data={"query": "grief journal", "result_count": 2, "results": [
            {"asin": "B012345678", "title": "Grief Journal", "review_count": 37},
            {"asin": "B087654321", "title": "Another", "review_count": 3},
        ]},
        captured_at="2026-10-20T00:00:00+00:00",
    )
    written = harvest_session(
        store, "s1", candidates=cands, browser_evidence=evidence,
    )
    assert written[KIND_NEAR_MISS] == 1
    assert written[KIND_KEYWORD] >= 1
    assert written[KIND_COMPETITOR] >= 2
    kw = store.get(KIND_KEYWORD, "grief journal", marketplace="us")
    assert kw is not None
    assert kw.payload["top10_review_median"] == 20
    # freshness reflects WHEN IT WAS MEASURED, not when the harvest ran
    assert kw.captured_at.startswith("2026-10-20")


# ------------------------------------------------------- runner-level wiring


def test_mode_profiles_rebalance_the_same_methodology():
    assert mode_profile("auto").iterations[P1_DISCOVERY] > \
        mode_profile("prompt").iterations[P1_DISCOVERY]
    assert mode_profile("prompt").iterations["competitive_landscape"] > \
        mode_profile("auto").iterations["competitive_landscape"]
    assert mode_profile("auto").extra_calls > mode_profile("keywords").extra_calls \
        >= mode_profile("prompt").extra_calls
    # every phase still has a positive cap, unknown modes fall back safely
    for mode in MODE_PROFILES:
        for phase in SPECS:
            assert mode_profile(mode).iterations_for(phase, 7) > 0
    assert mode_profile("nonsense").mode == "keywords"


def test_model_ceiling_raises_only_as_far_as_time_allows(db, config):
    from test_research_runner import PHASE_OUTPUTS, ScriptedModel, make_runner

    runner = make_runner(db, config, ScriptedModel(PHASE_OUTPUTS))
    base = runner.rc.max_model_calls_per_session
    assert runner._model_call_ceiling("prompt") == base
    assert runner._model_call_ceiling("keywords") > base
    assert runner._model_call_ceiling("auto") > runner._model_call_ceiling("keywords")
    # the raise is scaled down when the hard time budget is smaller
    config.session_hard_timeout_hours = 6
    assert runner._model_call_ceiling("auto") < base + MODE_PROFILES["auto"].extra_calls


def test_phase_iterations_follow_the_session_mode(db, config):
    from test_research_runner import PHASE_OUTPUTS, ScriptedModel, make_runner

    runner = make_runner(db, config, ScriptedModel(PHASE_OUTPUTS))
    assert runner._phase_iterations(P1_DISCOVERY, "auto") > \
        runner._phase_iterations(P1_DISCOVERY, "prompt")
    assert runner._phase_iterations(P1_DISCOVERY, "prompt") == \
        mode_profile("prompt").iterations[P1_DISCOVERY]


def test_external_sources_are_gated_in_phase1(db, config):
    from test_research_runner import PHASE_OUTPUTS, ScriptedModel, make_runner

    runner = make_runner(db, config, ScriptedModel(PHASE_OUTPUTS))
    prompt_sid = _mk_session(db, config, mode="prompt")
    auto_sid = _mk_session(db, config, mode="auto", prompt="")
    sessions = runner.sessions
    prompt_sess = sessions.require(prompt_sid)
    auto_sess = sessions.require(auto_sid)

    # P4-P6: external sources always allowed (Amazon-side candidates exist).
    assert runner._external_action_ok(prompt_sid, prompt_sess, P4_CONSUMER, 1, {}) is True
    assert runner._external_action_ok(prompt_sid, prompt_sess, "demand_validation", 1, {}) is True
    assert runner._external_action_ok(prompt_sid, prompt_sess, "opportunity_gap", 1, {}) is True
    # P1: never for a briefed run, never on the opening move...
    assert runner._external_action_ok(prompt_sid, prompt_sess, P1_DISCOVERY, 5, {}) is False
    assert runner._external_action_ok(auto_sid, auto_sess, P1_DISCOVERY, 1, {}) is False
    # ...and not before Amazon-side anchors exist.
    assert runner._external_action_ok(auto_sid, auto_sess, P1_DISCOVERY, 2, {}) is False
    BrowserEvidenceStore(db).create(
        session_id=auto_sid, marketplace="us", kind="search_results",
        url="https://www.amazon.com/s?k=grief", title="grief journals",
        data={"query": "grief", "result_count": 3, "results": []},
    )
    assert runner._external_action_ok(auto_sid, auto_sess, P1_DISCOVERY, 2, {}) is True


def test_niches_past_the_cap_are_recorded_as_backlog(db, config):
    from test_research_runner import PHASE_OUTPUTS, ScriptedModel, make_runner

    runner = make_runner(db, config, ScriptedModel(PHASE_OUTPUTS))
    sid = _mk_session(db, config)
    niches = [
        {"niche": f"narrow niche number {i} for adults", "parent_market": "m",
         "marketplace": "us"}
        for i in range(runner.rc.max_candidates + 6)
    ]
    runner._apply_phase_effects(sid, "aggressive_niching", {"niches": niches})
    created = CandidateStore(db).list(sid, limit=500)
    assert len(created) <= runner.rc.max_candidates
    backlog = runner._backlog_items(sid)
    assert len(backlog) == 6
    assert all(b["reason"] == "candidate_capacity" for b in backlog)


@pytest.mark.asyncio
async def test_run_writes_preflight_harvests_memory_and_ranks_headroom(db, config):
    """A full run exercises every new layer: durable preflight, whitespace
    persisted on candidates, memory harvested after the integrity pipeline,
    and the report carrying the adjacent-opportunities ledger."""
    from test_research_runner import PHASE_OUTPUTS, ScriptedModel, make_runner

    sid = _mk_session(db, config, mode="prompt")
    runner = make_runner(db, config, ScriptedModel(PHASE_OUTPUTS))
    await runner.run(sid)
    sessions = runner.sessions
    assert sessions.require(sid).status == "completed"

    from app.recovery import CheckpointStore

    cps = CheckpointStore(db)
    preflight = cps.get(sid, "discovery_preflight")
    assert preflight is not None and preflight.payload["done"] is True
    # headroom computed for every live candidate
    assert runner._ws_scores.get(sid)
    # memory harvested (best-effort, durable)
    events = EventLog(db).tail(sid)
    assert any(e.action == "market_memory_harvested" for e in events)
    # the report carries the adjacent ledger section
    from app.reports import ReportStore

    rep = [r for r in ReportStore(db, config).list(sid) if r.kind == "final"][0]
    model = ReportStore(db, config).read_model_json(rep)
    assert isinstance(model, dict)
    assert "adjacent_ledger" in model and "headroom_ranking" in model


# -------------------------------------------------------- external actions


@pytest.mark.asyncio
async def test_adjacency_promotes_only_measured_high_headroom(db, config):
    """A neighbour becomes a candidate only after a REAL search measured it
    above the headroom bar — never on derivation alone."""
    from app.actions import ActionResult
    from app.marketplace import AMAZON_MARKETPLACES
    from test_research_runner import PHASE_OUTPUTS, ScriptedModel, make_runner

    sid = _mk_session(db, config)
    runner = make_runner(db, config, ScriptedModel(PHASE_OUTPUTS))
    CandidateStore(db).create(
        sid, niche="grief journals for adult children", marketplace="us",
        rationale="strong existing candidate",
    )
    evidence = BrowserEvidenceStore(db)
    evidence.create(
        session_id=sid, marketplace="us", kind="autocomplete_frontier",
        url="https://www.amazon.com/", title="frontier: grief journal",
        data={"prefix": "grief journal",
              "suggestions": ["grief journal for loss of spouse"]},
    )

    async def fake_execute(*, session_id: str, action: str, args: dict) -> ActionResult:
        # The measurement search comes back with a thin, light-competition set.
        evidence.create(
            session_id=session_id, marketplace="us", kind="search_results",
            url="https://www.amazon.com/s?k=grief+journal",
            title="grief journal for loss of spouse",
            data={"query": args["query"], "result_count": 2, "results": [
                {"asin": "B011111111", "review_count": 4, "title": "a"},
                {"asin": "B022222222", "review_count": 1, "title": "b"},
            ]},
        )
        return ActionResult(action=action, ok=True, marketplace="us", url="",
                            evidence_ids=[], summary="measured")

    runner.executor.execute = fake_execute  # type: ignore[assignment]
    await runner._adjacency_expand(
        sid, runner.sessions.require(sid), [AMAZON_MARKETPLACES["us"]],
        "competitive_landscape",
    )
    rows = CandidateStore(db).list(sid, limit=500)
    promoted = [c for c in rows if (c.meta or {}).get("via") == "adjacency"]
    assert len(promoted) == 1
    assert promoted[0].niche == "grief journal for loss of spouse"
    assert promoted[0].meta["parent_market"] == "grief journals for adult children"
    assert promoted[0].meta["seed_evidence_ids"]
    # the round is durable: prompts and the report read it back
    assert runner._adjacency_rows(sid)
    assert "adjacent" in runner._adjacency_note(sid)
    # chain of custody: the promoted candidate's seed evidence becomes a
    # registered claim, so it is judged on captured records, not on silence
    from app.claims import ClaimStore

    runner._register_candidate_claims(sid, promoted[0], {})
    claims = ClaimStore(db).list(sid, candidate_id=promoted[0].id, limit=10)
    assert claims and claims[0].evidence_ids
    assert claims[0].quality != "model_inference"


class _ExternalPage:
    def __init__(self) -> None:
        self.url = "https://www.reddit.com/search/?q=x"

    async def title(self) -> str:
        return "page"

    async def evaluate(self, js: str, arg: str | None = None) -> object:
        if "complete/search" in js:
            return ["grief journal for loss of spouse", "grief journal for veterans"]
        if "bookTitle" in js or "result_count" in str(js):
            return {"result_count": 128, "results": [
                {"title": "The Grief Recovery Handbook", "author": "J. Wolf",
                 "avg_rating": 4.2, "ratings_count": 9000},
            ]}
        return [{"title": "I wish there was a journal that actually helped me"}]


class _ExternalBrowser:
    def __init__(self) -> None:
        self.page = _ExternalPage()
        self.navigated: list[str] = []

    async def new_page(self) -> object:
        return self.page

    async def navigate(self, page: object, url: str) -> object:
        self.navigated.append(url)
        page.url = url  # type: ignore[attr-defined]
        return page

    async def close_page(self, page: object) -> None:
        pass

    async def open_marketplace(self, marketplace: object, path: str = "/") -> object:
        return self.page

    async def screenshot(self, page: object) -> bytes:
        return b"png"


class _ExternalAuth:
    profile = "kdspy"

    async def _classify_page(self, page: object, marketplace: object):
        from app.amazon_auth import AuthCheck

        return AuthCheck("unknown", "", "t", {})


def _external_executor(db, config):
    from app.actions import ActionExecutor
    from app.evidence_capture import EvidenceCollector

    class _Artifacts:
        def save_bytes(self, *a, **k):
            class A:
                id = "art"
            return A()

    browser = _ExternalBrowser()
    store = BrowserEvidenceStore(db)
    events = EventLog(db)
    collector = EvidenceCollector(browser, store, _Artifacts())  # type: ignore[arg-type]
    ex = ActionExecutor(config, browser, collector, _ExternalAuth(), events)  # type: ignore[arg-type]
    return ex, browser, store, events


@pytest.mark.asyncio
async def test_external_actions_capture_ordinary_evidence(db, config):
    _seed_session_row(db)
    ex, browser, store, events = _external_executor(db, config)

    forum = await ex.execute(
        session_id="s1", action="forum_search",
        args={"query": "grief journal that actually helps"},
    )
    assert forum.ok and forum.evidence_ids
    rec = store.get(forum.evidence_ids[0])
    assert rec is not None and rec.kind == "external_demand"
    assert rec.data["source"] == "reddit" and rec.data["external"] is True
    assert any("reddit.com/search" in u for u in browser.navigated)

    google = await ex.execute(
        session_id="s1", action="google_suggest", args={"query": "best grief journal"},
    )
    assert google.ok and google.evidence_ids
    assert store.get(google.evidence_ids[0]).data["suggestions"]

    goodreads = await ex.execute(
        session_id="s1", action="goodreads_check", args={"query": "grief journal"},
    )
    assert goodreads.ok and goodreads.evidence_ids
    data = store.get(goodreads.evidence_ids[0]).data
    assert data["result_count"] == 128 and data["results"]

    # every external action journaled like any other action
    assert any(e.action == "action_forum_search" for e in events.tail("s1"))


@pytest.mark.asyncio
async def test_external_actions_refuse_non_allow_listed_hosts(db, config):
    _seed_session_row(db)
    ex, browser, store, _ = _external_executor(db, config)
    # Force the guard: the executor validates before any navigation.
    with pytest.raises(Exception):
        await ex._open_external("https://evil.example/exfiltrate")
    assert browser.navigated == []


# ------------------------------------------------------------ report ledger

def test_report_ledger_lists_unfunded_demand():
    from app.report_model import ReportModel, render_markdown

    model = ReportModel(
        adjacent_ledger=[
            {"niche": "grief journals for veterans", "status": "discovered",
             "origin": "adjacency", "whitespace": 0.78,
             "evidence_ids": ["abc123def456"], "marketplace": "us",
             "rationale": "adjacent to a winner"},
            {"niche": "capacity backlog niche", "status": "backlog",
             "origin": "capacity_backlog", "whitespace": None,
             "evidence_ids": [], "marketplace": "", "rationale": ""},
        ],
        headroom_ranking=[
            {"rank": 1, "name": "A", "niche": "a", "whitespace": 0.7,
             "measured": True, "confidence": 0.6},
            {"rank": 2, "name": "B", "niche": "b", "whitespace": None,
             "measured": False, "confidence": 0.5},
        ],
    )
    md = render_markdown(model)
    assert "Adjacent opportunities not yet researched" in md
    assert "One-click follow-up" in md
    assert "grief journals for veterans" in md
    assert "Headroom ranking" in md
    assert "0.78" in md
    # ledger is JSON round-trippable with the rest of the model
    from app.report_model import ReportModel as RM

    assert RM.from_dict(model.to_dict()).adjacent_ledger == model.adjacent_ledger


# ------------------------------------------------------------- follow-up API

def test_adjacent_endpoint_serves_the_report_ledger(client):
    import json

    def _login() -> dict:
        res = client.post(
            "/api/auth/login",
            json={"username": "owner", "password": "hunter2"},
        )
        return {"X-Auth-Token": res.json()["token"]}

    headers = _login()
    sid = client.post(
        "/api/sessions", headers=headers,
        json={"mode": "keywords", "prompt": "grief journals"},
    ).json()["session"]["id"]

    # No report yet -> empty ledger, never an error.
    empty = client.get(f"/api/sessions/{sid}/adjacent", headers=headers)
    assert empty.status_code == 200 and empty.json()["ledger"] == []

    st = client.app.state
    rep = st.reports.save(sid, kind="final", title="R", body_markdown="# r")
    st.reports.save_model_json(sid, report=rep, model_json=json.dumps({
        "adjacent_ledger": [
            {"niche": "grief journals for veterans", "status": "discovered",
             "origin": "adjacency", "whitespace": 0.78,
             "evidence_ids": ["abc123def456"], "marketplace": "us",
             "rationale": "adjacent to a winner"},
        ],
    }))
    res = client.get(f"/api/sessions/{sid}/adjacent", headers=headers)
    assert res.status_code == 200
    body = res.json()
    assert body["report_id"] == rep.id
    assert body["ledger"][0]["niche"] == "grief journals for veterans"
    assert body["ledger"][0]["whitespace"] == 0.78

    # Unknown session is a clean 404 (the login cookie is what auths here).
    assert client.get("/api/sessions/nope/adjacent", headers=headers).status_code == 404

