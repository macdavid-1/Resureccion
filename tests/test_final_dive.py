"""Final-candidate deep dive, overlay dismissal and the search-result count.

These three features are one chain: the shipping-country overlay is
dismissed automatically so Amazon's printed result total is readable, that
total is captured as evidence, and the report's finalists then get their
backend keywords and both store category maps measured from real pages.
"""
from __future__ import annotations

import pytest

from app.browser_manager import _OVERLAY_DISMISS_JS, dismiss_amazon_overlays
from app.evidence_capture import _extract_results_summary
from app.final_dive import FinalCandidateDive, competition_label
from app.marketplace import AMAZON_MARKETPLACES


# --------------------------------------------------------------------- fakes
class _El:
    def __init__(self, text: str = "") -> None:
        self._text = text

    async def inner_text(self) -> str:
        return self._text

    async def get_attribute(self, _name: str):
        return None


class _Card:
    def __init__(self, asin: str, title: str, reviews: int) -> None:
        self._asin = asin
        self._title = title
        self._reviews = reviews

    async def get_attribute(self, name: str):
        return self._asin if name == "data-asin" else None

    async def query_selector(self, sel: str):
        if "h2" in sel:
            return _El(self._title)
        if "offscreen" in sel:
            return _El("$9.99")
        if "icon-alt" in sel:
            return _El("4.5 out of 5 stars")
        return _El(str(self._reviews))


class _Page:
    """Search page, product page or suggestion page — shaped by its URL."""

    def __init__(self, url: str, *, body: str = "", cards=(), breadcrumbs=(),
                 product: dict | None = None, suggestions=()) -> None:
        self.url = url
        self._body = body
        self._cards = list(cards)
        self._breadcrumbs = list(breadcrumbs)
        self._product = product or {}
        self._suggestions = list(suggestions)

    async def inner_text(self, sel: str) -> str:
        if sel == "body":
            return self._body
        if sel == "#productTitle":
            return str(self._product.get("title", ""))
        if sel.startswith("#acrCustomerReviewText"):
            return str(self._product.get("review_text", ""))
        if "detail" in sel or "SalesRank" in sel:
            return str(self._product.get("bsr", ""))
        if "acrPopover" in sel:
            return str(self._product.get("rating", ""))
        return ""

    async def query_selector(self, sel: str):
        if "productTitle" in sel:
            return _El(str(self._product.get("title", "")))
        if "offscreen" in sel:
            return _El(str(self._product.get("price", "")))
        if "acrPopover" in sel:
            return _El(str(self._product.get("rating", "")))
        if "acrCustomerReviewText" in sel:
            return _El(str(self._product.get("review_text", "")))
        if "detail" in sel or "SalesRank" in sel:
            return _El(str(self._product.get("bsr", "")))
        return None

    async def query_selector_all(self, sel: str):
        if "s-search-result" in sel:
            return list(self._cards)
        if "wayfinding-breadcrumbs" in sel:
            return [_El(c) for c in self._breadcrumbs]
        return []

    async def evaluate(self, _js: str, arg=None):
        base = str(arg or "").strip()
        if not base:
            return []
        return [f"{base} workbook", f"{base} for kids"]


class _DiveBrowser:
    """Hands out the right page for each path the dive asks for."""

    def __init__(self) -> None:
        self.closed = 0
        self.navigations: list[str] = []
        self._books_pages = 0

    async def open_marketplace(self, mkt, path: str = "/"):
        if path == "/":
            return _Page(f"{mkt.base_url}/")
        if path.startswith("/s?"):
            return self._search(f"{mkt.base_url}{path}", path)
        return _Page(f"{mkt.base_url}{path}")

    def _search(self, url: str, path: str):
        kindle = "i=digital-text" in path
        prefix = "B00000000" if kindle else "012345678"
        cards = [
            _Card(f"{prefix}{i}", f"Book {i} for search", 10 * (i + 1))
            for i in range(3)
        ]
        body = "1-16 of over 4,000 results for space facts"
        return _Page(url, body=body, cards=cards)

    async def navigate(self, page, url: str):
        self.navigations.append(url)
        if "/dp/" not in url:
            return page
        asin = url.rstrip("/").split("/dp/")[-1]
        kindle = asin.startswith("B0")
        return _Page(
            url,
            breadcrumbs=(
                ["Kindle Store", "Kindle eBooks", "Children's eBooks"]
                if kindle else
                ["Books", "Children's Books", "Science, Nature & How It Works"]
            ),
            product={
                "title": f"Product {asin}",
                "price": "$9.99",
                "rating": "4.5 out of 5 stars",
                "review_text": "1,234 reviews",
                "bsr": "#123 in Children's Books (Books)",
            },
        )

    async def close_page(self, _page) -> None:
        self.closed += 1


# ----------------------------------------------------------- overlay dismissal
def test_dismiss_script_only_ever_clicks_refusal_labels() -> None:
    """Research must never ACCEPT anything on the owner's behalf.

    The click set is the only thing that decides what gets dismissed, so it
    is asserted literally: refusal labels in, acceptance labels impossible.
    """
    import re

    m = re.search(r"REFUSALS = new Set\(\[(.*?)\]\)", _OVERLAY_DISMISS_JS)
    assert m, "the dismiss script must declare an explicit click set"
    labels = m.group(1)
    assert "'dismiss'" in labels and "'no thanks'" in labels
    for forbidden in ("change address", "accept", "continue", "submit", "buy"):
        assert forbidden not in labels


@pytest.mark.asyncio
async def test_dismiss_returns_labels_and_waits_for_the_animation() -> None:
    class Page:
        def __init__(self) -> None:
            self.waited = 0

        async def evaluate(self, _js):
            return ["dismiss", "dismiss"]

        async def wait_for_timeout(self, ms: int) -> None:
            self.waited = ms

    page = Page()
    assert await dismiss_amazon_overlays(page) == ["dismiss", "dismiss"]
    assert page.waited > 0


@pytest.mark.asyncio
async def test_dismiss_never_raises_on_a_page_without_the_overlay() -> None:
    class Page:
        async def evaluate(self, _js):
            return []

    class Broken:
        async def evaluate(self, _js):
            raise RuntimeError("no such frame")

    assert await dismiss_amazon_overlays(Page()) == []
    assert await dismiss_amazon_overlays(Broken()) == []


# --------------------------------------------------------- result count capture
@pytest.mark.asyncio
async def test_results_summary_reads_amazons_printed_total() -> None:
    class Page:
        def __init__(self, body: str) -> None:
            self._body = body

        async def inner_text(self, _sel: str) -> str:
            return self._body

    out = await _extract_results_summary(
        Page("1-16 of over 4,000 results for space facts book for kids")
    )
    assert out["total_results"] == 4000
    assert out["total_results_are_approximate"] is True
    assert "4,000" in out["results_text"]

    exact = await _extract_results_summary(Page("1-16 of 238 results for x"))
    assert exact["total_results"] == 238
    assert exact["total_results_are_approximate"] is False

    # Some layouts print the total without the "1-16 of" range prefix.
    bare = await _extract_results_summary(Page("over 7,000,000 results for books"))
    assert bare["total_results"] == 7_000_000
    assert bare["total_results_are_approximate"] is True


@pytest.mark.asyncio
async def test_results_summary_never_invents_a_missing_number() -> None:
    class Page:
        async def inner_text(self, _sel: str) -> str:
            # Region-dependent rendering: the word prints, the figure does not.
            return "1-16 of over results for space facts book for kids"

    out = await _extract_results_summary(Page())
    assert "total_results" not in out
    assert out["total_results_are_approximate"] is True
    assert "results" in out["results_text"]

    class Empty:
        async def inner_text(self, _sel: str) -> str:
            return "no numbers here"

    assert await _extract_results_summary(Empty()) == {}


def test_competition_labels_are_plain_language() -> None:
    assert competition_label(1_200) == "low"
    assert competition_label(40_000) == "high"
    assert competition_label(None) == "not measured"


# ------------------------------------------------------------------- the dive
@pytest.mark.asyncio
async def test_dive_measures_keywords_and_both_store_category_trees(db, config):
    from app.browser_store import BrowserEvidenceStore

    _seed(db)
    store = BrowserEvidenceStore(db)
    dive = FinalCandidateDive(
        _DiveBrowser(), store,
        max_keywords=6, max_categories=3, autocomplete_queries=12,
        products_per_store=2, time_budget_seconds=60,
    )
    res = await dive.run(
        session_id="s1",
        marketplaces=[AMAZON_MARKETPLACES["us"]],
        niche="space facts books for kids",
        candidate_id="cand1",
    )
    assert res.error is None, res.error

    # --- keywords: measured against Amazon's own printed total -----------
    assert res.keywords, "no backend keywords shortlisted"
    assert res.keywords_measured >= 1
    assert len(res.keywords) <= 6
    measured = [k for k in res.keywords if k.get("measured")]
    assert measured
    for k in measured:
        assert k["total_results"] == 4000
        assert k["competition"] == "low"
        assert k["department"] == "books"
        assert k["top10_review_median"] == 20
    assert any(k.get("demand_score", 0) > 0 for k in measured)

    # --- categories: Kindle Store and Books are separate systems ---------
    assert len(res.books_categories) <= 3
    assert len(res.kindle_categories) <= 3
    assert res.books_categories and res.kindle_categories
    assert {c["store"] for c in res.books_categories} == {"books"}
    assert {c["store"] for c in res.kindle_categories} == {"kindle"}
    assert res.books_categories[0]["category"].startswith("Science")
    assert res.kindle_categories[0]["category"] == "Children's eBooks"
    assert res.books_categories[0]["breadcrumb"].startswith("Books >")
    assert res.kindle_categories[0]["breadcrumb"].startswith("Kindle Store >")

    # --- evidence: durable rows the report and gates read ----------------
    kinds = {e.kind for e in store.list("s1")}
    assert "keyword_research" in kinds and "category_research" in kinds
    assert len(res.evidence_ids) == 2
    kw_row = next(e for e in store.list("s1") if e.kind == "keyword_research")
    assert kw_row.data["requested"] == 6
    assert kw_row.data["measured"] == res.keywords_measured
    cat_row = next(e for e in store.list("s1") if e.kind == "category_research")
    assert cat_row.data["books"] and cat_row.data["kindle_store"]


@pytest.mark.asyncio
async def test_dive_respects_the_keyword_cap_and_never_raises(db, config):
    from app.browser_store import BrowserEvidenceStore

    _seed(db)

    class _DeadBrowser(_DiveBrowser):
        async def open_marketplace(self, mkt, path: str = "/"):
            raise RuntimeError("browser is down")

    res = await FinalCandidateDive(
        _DeadBrowser(), BrowserEvidenceStore(db),
        max_keywords=4, time_budget_seconds=30,
    ).run(
        session_id="s1",
        marketplaces=[AMAZON_MARKETPLACES["us"]],
        niche="space facts books",
        candidate_id="c1",
    )
    assert res.error is not None          # reported, not raised
    assert res.keywords == []
    assert not res.done


def _seed(db) -> None:
    with db.tx() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO research_sessions (id, name, status, mode, "
            "created_at, updated_at, last_activity_at) "
            "VALUES ('s1', 'n', 'running', 'prompt', '2025-01-01', '2025-01-01', '2025-01-01')"
        )


# ------------------------------------------------------- report integration
def test_report_renders_measured_backend_keywords_and_both_stores() -> None:
    from app.report_model import ReportModel, render_markdown

    model = ReportModel(
        session={"id": "s1", "name": "Run", "mode": "prompt"},
        executive_summary={},
        opportunities=[{
            "name": "Space Facts", "niche": "space facts books for kids",
            "confidence": 0.8, "market_gap": "gap", "differentiation": "diff",
            "verification": {"status": "passed", "verdict": "verified",
                             "confidence": 0.8},
            "backend_research": {
                "department": "books",
                "ranked_by": "buyer demand first, then lightest competition",
                "keyword_evidence_id": "ev_kw_1",
                "category_evidence_id": "ev_cat_1",
                "keywords": [
                    {"keyword": "space facts for kids", "measured": True,
                     "total_results": 4000, "total_results_are_approximate": True,
                     "competition": "low", "top10_review_median": 20,
                     "demand_score": 55},
                    {"keyword": "space facts workbook", "measured": False,
                     "demand_score": 40},
                ],
                "books": [{"store": "books", "category": "Science",
                           "breadcrumb": "Books > Children's Books > Science",
                           "asin": "0123456789"}],
                "kindle_store": [{"store": "kindle",
                                  "category": "Children's eBooks",
                                  "breadcrumb": "Kindle Store > Kindle eBooks > Children's eBooks",
                                  "asin": "B000000001"}],
                "category_note": "Kindle Store and Books keep separate category trees.",
            },
        }],
        quality_statement={"certainty_policy": "evidence only"},
    )
    md = render_markdown(model)
    assert "Backend keywords researched for this niche" in md
    assert "space facts for kids" in md
    assert "over 4,000" in md
    assert "low" in md
    assert "not searched in this run" in md
    assert "Store categories researched for this niche" in md
    assert "Books store" in md and "Kindle Store" in md
    assert "Children's eBooks" in md
    # The PDF path carries the same facts.
    from app.pdf_export import model_to_blocks, _kw_total_txt

    _, _, blocks = model_to_blocks(model.to_dict())
    text = " ".join(str(b.get("x", "")) for b in blocks if b.get("t") in ("p", "h3"))
    assert "Backend keywords researched for this niche" in text
    assert _kw_total_txt({"measured": True, "total_results": 4000,
                          "total_results_are_approximate": True}) == "over 4,000"


@pytest.mark.asyncio
async def test_finalist_deep_dive_runs_once_per_report_candidate(
    db, config, monkeypatch
):
    """The runner dives the candidates that actually reach the report, in
    report order, and reuses the durable checkpoint on a later run."""
    from app.agent import AgentStateStore
    from app.artifacts import ArtifactStore
    from app.browser_store import BrowserEvidenceStore
    from app.events import EventLog
    from app.final_dive import DiveResult
    from app.recovery import CheckpointStore, ErrorStateStore
    from app.research_data import (
        CandidateStore,
        EvidenceStore,
        ObservationStore,
        OpportunityStore,
    )
    from app.research_runner import ResearchRunner
    from app.sessions import SessionStore
    from app.trace import ActivityTrace

    events = EventLog(db)
    sessions = SessionStore(db, config)
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO research_sessions (id, name, status, mode, created_at, "
            "updated_at, last_activity_at) VALUES ('s1', 'n', 'running', 'prompt', "
            "'2025-01-01', '2025-01-01', '2025-01-01')"
        )
    cands = CandidateStore(db)
    with_opportunity = cands.create(
        "s1", niche="space facts books for kids", marketplace="us"
    )
    plain = cands.create("s1", niche="moon facts workbooks", marketplace="us")
    dead = cands.create("s1", niche="unresearched niche", marketplace="us")
    cands.set_status("s1", with_opportunity.id, "verified")
    cands.set_status("s1", plain.id, "verified")
    cands.set_status("s1", dead.id, "rejected")
    OpportunityStore(db).create(
        "s1", candidate_id=with_opportunity.id, title="Space Facts",
        niche="space facts books for kids", marketplace="us", angle="angle",
        keywords=[], confidence=0.7, meta={},
    )

    calls: list[dict] = []

    class _StubDive:
        last_kwargs: dict = {}

        def __init__(self, *args, kwargs_unused=None, **kwargs) -> None:
            _StubDive.last_kwargs = kwargs

        async def run(self, *, session_id, marketplaces, niche, candidate_id=""):
            calls.append({
                "candidate_id": candidate_id, "niche": niche,
                "marketplaces": [m.code for m in marketplaces],
            })
            return DiveResult(candidate_id=candidate_id, niche=niche)

    monkeypatch.setattr("app.research_runner.FinalCandidateDive", _StubDive)

    runner = ResearchRunner(
        db, config,
        sessions=sessions, agent=AgentStateStore(db, events), events=events,
        errors=ErrorStateStore(db), checkpoints=CheckpointStore(db),
        candidates=cands, evidence=EvidenceStore(db),
        observations=ObservationStore(db), opportunities=OpportunityStore(db),
        artifacts=ArtifactStore(db, config), browser=object(),  # type: ignore[arg-type]
        browser_evidence=BrowserEvidenceStore(db),
        browser_auth=object(),  # type: ignore[arg-type]
        model=object(),  # type: ignore[arg-type]
        trace=ActivityTrace(db),
    )

    # The opportunity's candidate leads (it is the report's portfolio), and
    # rejected candidates never get researched.
    targets = runner._final_candidates("s1")
    assert [c.id for c in targets] == [with_opportunity.id, plain.id]

    await runner._final_deep_dive("s1")
    assert [c["candidate_id"] for c in calls] == [
        with_opportunity.id, plain.id
    ]
    # The dive gets the session's marketplace plan (it measures on the first).
    assert calls[0]["marketplaces"] and "us" in calls[0]["marketplaces"]
    # Config knobs reach the dive.
    assert _StubDive.last_kwargs["max_keywords"] == config.dive_max_keywords
    assert _StubDive.last_kwargs["max_categories"] == config.dive_max_categories

    # Durable: the checkpoint marks each finalist done …
    cp = runner.checkpoints.get_safe("s1", f"final_dive:{with_opportunity.id}")
    assert cp is not None and cp.payload.get("done") is True
    assert cp.payload.get("niche") == "space facts books for kids"

    # … so a resumed report never re-walks them.
    calls.clear()
    await runner._final_deep_dive("s1")
    assert calls == []


def test_dive_can_be_switched_off_entirely(db, config, monkeypatch) -> None:
    config.dive_enabled = False
    from app.final_dive import FinalCandidateDive

    async def _boom(*a, **k):
        raise AssertionError("the dive must not run when disabled")

    monkeypatch.setattr(FinalCandidateDive, "run", _boom)
    assert getattr(config, "dive_enabled") is False


def test_dive_evidence_is_indexed_by_niche_for_the_report() -> None:
    from app.report_model import _dive_index

    rows = [
        {"id": "ev_kw", "kind": "keyword_research",
         "data": {"niche": "Space Facts Books", "keywords": [{"keyword": "x"}],
                  "department": "books"}},
        {"id": "ev_cat", "kind": "category_research",
         "data": {"niche": "space facts books", "books": [{"category": "A"}],
                  "kindle_store": [{"category": "B"}]}},
        {"id": "ev_other", "kind": "search_results", "data": {"query": "y"}},
    ]
    idx = _dive_index(rows)
    assert list(idx) == ["space facts books"]
    slot = idx["space facts books"]
    assert slot["keyword_evidence_id"] == "ev_kw"
    assert slot["category_evidence_id"] == "ev_cat"
    assert slot["books"][0]["category"] == "A"
    assert slot["kindle_store"][0]["category"] == "B"
