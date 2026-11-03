"""Per-final-candidate report sections: measured backend data + model prose.

The owner's requirement: every final candidate gets its own report section —
10 Kindle Store categories and 10 Books store categories (two separate Amazon
catalogue trees), 20 high-ranking low-competition keywords, ten premium titles
the model writes from those keywords, the marketplace order with the one(s)
most likely to perform, and what to do better than the incumbents based on
their reviews. The raw figures must come from the backend (live measurement),
the writing from the model — and the model must never be able to invent a
figure or justify a title with a keyword that was not measured.

These tests pin each link in that chain, including the end-to-end report.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from tests.test_research_runner import session  # noqa: F401 — pytest fixture reuse

NICHE = "grief journals for adult daughters"
OTHER = "space facts books for curious children"


# --------------------------------------------------------------- fixtures
def _keywords(niche: str, n: int = 22) -> list[dict[str, Any]]:
    rows = []
    for i in range(n):
        measured = i < 20
        total = (4000, 12000, 40000)[i % 3]
        rows.append({
            "keyword": f"{niche} phrase {i}",
            "measured": measured,
            "total_results": total if measured else None,
            "competition": ("low", "medium", "high")[i % 3] if measured else None,
            "top10_review_median": 10 + i,
            "demand_score": 100 - i,
            "marketplace": "uk",
        })
    return rows


def _dive_rows(niche: str) -> list[dict[str, Any]]:
    """The three evidence records the finalist dive writes, per candidate."""
    books = [
        {
            "store": "books",
            "category": f"Books category {i}",
            "breadcrumb": f"Books > Niche > Books category {i}",
            "asin": f"01{i:08d}",
        }
        for i in range(12)
    ]
    kindle = [
        {
            "store": "kindle",
            "category": f"Kindle category {i}",
            "breadcrumb": f"Kindle Store > Kindle eBooks > Kindle category {i}",
            "asin": f"B0{i:08d}",
        }
        for i in range(12)
    ]
    return [
        {
            "id": "ev_kw",
            "kind": "keyword_research",
            "marketplace": "uk",
            "url": "https://www.amazon.co.uk/s",
            "data": {
                "niche": niche,
                "department": "books",
                "requested": 20,
                "ranked_by": "lightest competition first, then buyer demand",
                "keywords": _keywords(niche),
            },
        },
        {
            "id": "ev_cat",
            "kind": "category_research",
            "marketplace": "uk",
            "url": "https://www.amazon.co.uk/s",
            "data": {
                "niche": niche,
                "books": books,
                "kindle_store": kindle,
                "note": "Kindle Store and Books keep separate category trees",
            },
        },
        {
            "id": "ev_rev",
            "kind": "review_research",
            "marketplace": "uk",
            "url": "https://www.amazon.co.uk/s",
            "data": {
                "niche": niche,
                "products_read": 3,
                "reviews": [{
                    "asin": "0100000001",
                    "rating": 5,
                    "title": "Loved it",
                    "body": "The prompts helped me write again.",
                }],
            },
        },
    ]


def _candidate(cid: str, niche: str, *, marketplace: str = "uk", status: str = "verified"):
    return SimpleNamespace(
        id=cid, niche=niche, marketplace=marketplace, status=status, meta={},
    )


def _opportunity(cid: str, niche: str, title: str = "A Portfolio Opportunity"):
    return SimpleNamespace(
        id=f"opp_{cid}", candidate_id=cid, title=title, niche=niche,
        angle="positioning angle", keywords=[], confidence=0.8,
        meta={"title_concepts": ["Held: A Grief Journal"], "differentiation": "diff"},
    )


def _sections(**over: Any) -> list[dict[str, Any]]:
    from app.report_model import _dive_index, _finalist_sections

    cand_a = _candidate("c1", NICHE)
    cand_b = _candidate("c2", OTHER)
    rejected = _candidate("c3", "an unrelated rejected niche", status="rejected")
    ev = _dive_rows(NICHE)
    kwargs: dict[str, Any] = {
        "cands": [cand_a, cand_b, rejected],
        "opps": [_opportunity("c1", "grief journals for adult children")],
        "dive_by_niche": _dive_index(ev),
        "cons_out": [{
            "niche": "grief journals for adult",
            "praises": ["gentle prompts"],
            "complaints": ["too much filler and fluff"],
            "unmet": ["nothing for sudden loss"],
            "design_opportunities": ["add a sudden-loss section"],
        }],
        "gap_out": [{
            "niche": "grief journals for adult",
            "readers_want": "short, usable prompts",
            "competitors_fail": "filler pages",
        }],
        "competitors": [{
            "title": "Grief Journal", "niche": "grief journals for adult",
            "marketplace": "uk", "price": "£7.99", "review_count": 210, "rank": 4,
        }],
        # Phase-7 labels are their own wording — the match must survive drift.
        "xmarket_out": [
            {"niche": "grief journals for adult", "marketplace": "us",
             "verdict": "present", "notes": "same niche ranked in the US"},
            {"niche": "grief journals for adult", "marketplace": "de",
             "verdict": "absent", "notes": "no comparable bestseller found"},
        ],
    }
    kwargs.update(over)
    return _finalist_sections(**kwargs)


def _section_one() -> dict[str, Any]:
    return _sections()[0]


def _playbook(section: dict[str, Any], *, count: int = 10) -> dict[str, Any]:
    kw = (
        section["keywords"][0]["keyword"]
        if section["keywords"] else section["niche"]
    )
    return {
        "titles": [
            {
                "title": f"Grief Journal Title {i}",
                "subtitle": f"A gentle, usable journal for daughters — edition {i}",
                "targets_keyword": kw,
                "why_it_ranks": "It matches the measured phrase exactly.",
                "why_it_converts": "It names the reader and the promise.",
            }
            for i in range(count)
        ],
        "marketplace_plan": "Publish on Amazon UK first, then the US.",
        "category_plan": "Start in the Kindle Store tree, mirror it in Books.",
        "keyword_plan": "Lead with the lowest-competition phrase in the title.",
        "beating_incumbents": "Cut the filler and add a sudden-loss section.",
    }


# ------------------------------------------------------- deterministic data
def test_finalist_section_delivers_10_plus_10_categories_and_20_ranked_keywords():
    sections = _sections()

    # Portfolio candidates lead; verified survivors follow; rejected never does.
    assert [s["candidate_id"] for s in sections] == ["c1", "c2"]

    sec = sections[0]
    assert sec["niche"] == "grief journals for adult children"  # the dossier label
    assert sec["candidate_niche"] == NICHE
    assert sec["marketplace_derived_from"] == "uk"
    assert sec["opportunity_title"] == "A Portfolio Opportunity"

    cats = sec["categories"]
    assert len(cats["kindle_store"]) == 10
    assert len(cats["books"]) == 10
    assert [r["rank"] for r in cats["books"]] == list(range(1, 11))
    assert [r["rank"] for r in cats["kindle_store"]] == list(range(1, 11))
    assert cats["books"][0]["breadcrumb"].startswith("Books >")
    assert cats["kindle_store"][0]["breadcrumb"].startswith("Kindle Store >")

    kws = sec["keywords"]
    assert len(kws) == 20
    assert [k["rank"] for k in kws] == list(range(1, 21))
    # Lowest competition first — that is what a new book can rank against.
    assert kws[0]["competition"] == "low"
    ranks = [
        {"low": 0, "medium": 1, "high": 2}[k["competition"]] for k in kws
    ]
    assert ranks == sorted(ranks)
    assert sec["keywords_measured"] == 20
    assert all(k["marketplace"] == "uk" for k in kws)


def test_finalist_section_ranks_marketplaces_from_evidence_only():
    ranking = _section_one()["marketplace_ranking"]
    assert [r["code"] for r in ranking] == ["uk", "us", "de"]
    assert [r["rank"] for r in ranking] == [1, 2, 3]
    assert "derived from" in ranking[0]["basis"]
    assert "confirmed" in ranking[1]["basis"]
    assert "not confirmed" in ranking[2]["basis"] or "checked" in ranking[2]["basis"]
    assert ranking[0]["name"] == "Amazon UK"
    assert ranking[0]["currency"] == "GBP"


def test_finalist_section_carries_live_review_material_and_title_seeds():
    sec = _section_one()
    findings = sec["review_findings"]
    assert findings["complaints"] == ["too much filler and fluff"]
    assert findings["unmet"] == ["nothing for sudden loss"]
    assert findings["readers_want"] == "short, usable prompts"
    assert findings["reviews_read"] == 1
    assert findings["products_read"] == 3
    assert findings["review_evidence_id"] == "ev_rev"
    assert findings["reviews"][0]["body"] == "The prompts helped me write again."
    assert sec["incumbents"] and sec["incumbents"][0]["title"] == "Grief Journal"
    # Titles are seeded from the measured keywords and the opportunity's own
    # concepts — never from nothing.
    assert "Held: A Grief Journal" in sec["title_seeds"]
    assert sec["keywords"][0]["keyword"] in sec["title_seeds"]


def test_finalist_sections_respect_the_cap_and_report_honest_absence():
    only_one = _sections(cap=1)
    assert len(only_one) == 1

    from app.report_model import _dive_index, _finalist_sections

    secs = _finalist_sections(
        cands=[_candidate("c9", "a niche the dive never reached")],
        opps=[],
        dive_by_niche=_dive_index([]),
        cons_out=[], gap_out=[], competitors=[], xmarket_out=[],
    )
    sec = secs[0]
    assert sec["keywords"] == [] and sec["categories"]["books"] == []
    assert sec["marketplace_ranking"][0]["code"] == "uk"
    assert sec["review_findings"]["source"] == "not captured in this session"
    assert any("never" in n or "did not reach" in n for n in sec["research_notes"])


# --------------------------------------------------------- model validation
def test_titles_must_target_a_measured_keyword_and_never_carry_a_statistic():
    from app.finalist_playbook import (
        TITLE_TARGET,
        build_playbook_digest,
        digest_json,
        normalize_playbook,
    )
    from app.report_narrative import allowed_numbers

    sec = _section_one()
    measured = sec["keywords"][0]["keyword"]
    allowed = allowed_numbers(digest_json(build_playbook_digest(sec)))

    good = [
        {
            "title": f"Gentle Grief Journal {i}",
            "subtitle": f"Prompts for daughters, volume {i}",
            "targets_keyword": measured,
            "why_it_ranks": "It matches a measured phrase with low competition.",
            "why_it_converts": "It names the reader and her situation.",
        }
        for i in range(TITLE_TARGET)
    ]
    content = {
        # The bad ones lead so the rejection path is exercised even though the
        # ten good titles already satisfy the target.
        "titles": [
            # Invented keyword: cannot be justified, so it is dropped.
            {"title": "A Book About Nothing", "targets_keyword": "unmeasured phrase"},
            # Fabricated statistic in the title: rejected outright.
            {"title": "How to Sell 12,438 Copies", "targets_keyword": measured},
            # Duplicate of the first good title: rejected when it arrives.
            {"title": "Gentle Grief Journal 0", "targets_keyword": measured},
        ] + good,
        "marketplace_plan": (
            "Publish on Amazon UK first. It reaches 99,999 buyers overnight."
        ),
        "category_plan": "Start with the Kindle Store tree.",
        "keyword_plan": "Lead with the lowest-competition phrase.",
        "beating_incumbents": "Answer the filler complaint with tighter chapters.",
    }
    playbook, violations = normalize_playbook(
        content, allowed=allowed, section=sec
    )
    titles = playbook["titles"]
    assert len(titles) == TITLE_TARGET
    assert all(t["targets_keyword"] == measured for t in titles)
    assert [t["title"] for t in titles] == [g["title"] for g in good]
    # The invented figure is stripped from the prose, sentence by sentence.
    assert "99,999" not in playbook["marketplace_plan"]
    assert "Publish on Amazon UK first." in playbook["marketplace_plan"]
    joined = " ".join(violations)
    assert "unmeasured phrase" in joined or "not measured" in joined
    assert "statistic" in joined


def test_no_review_material_means_no_beating_incumbents_plan():
    from app.finalist_playbook import (
        build_playbook_digest,
        digest_json,
        normalize_playbook,
    )
    from app.report_narrative import allowed_numbers

    sec = dict(_section_one())
    sec["review_findings"] = {
        "praises": [], "complaints": [], "unmet": [],
        "design_opportunities": [], "reviews": [], "source": "not captured",
    }
    allowed = allowed_numbers(digest_json(build_playbook_digest(sec)))
    playbook, _ = normalize_playbook(
        {
            "titles": _playbook(sec)["titles"],
            "beating_incumbents": "Write better chapters than everyone else.",
        },
        allowed=allowed,
        section=sec,
    )
    assert "beating_incumbents" not in playbook
    assert len(playbook["titles"]) == 10


def test_playbook_titles_report_a_shortfall_instead_of_padding():
    from app.finalist_playbook import (
        build_playbook_digest,
        digest_json,
        normalize_playbook,
    )
    from app.report_narrative import allowed_numbers

    sec = _section_one()
    allowed = allowed_numbers(digest_json(build_playbook_digest(sec)))
    playbook, _ = normalize_playbook(
        {"titles": _playbook(sec, count=3)["titles"]},
        allowed=allowed,
        section=sec,
    )
    assert len(playbook["titles"]) == 3
    assert playbook["titles_shortfall"] is True


# ------------------------------------------------------------- the document
def test_report_renders_one_section_per_final_candidate_with_the_prose():
    from app.report_model import ReportModel, render_markdown

    sections = _sections()
    model = ReportModel(
        session={"id": "s1", "name": "Run", "mode": "prompt"},
        executive_summary={},
        finalist_sections=sections,
        finalist_playbooks={
            s["candidate_id"]: _playbook(s) for s in sections
        },
    )
    md = render_markdown(model)

    assert "## Final candidate playbooks" in md
    assert "### Final candidate 1 — grief journals for adult children" in md
    assert "### Final candidate 2 — space facts books for curious children" in md
    assert "**Derived from marketplace:** uk (Amazon UK)" in md

    # 10 + 10 categories, numbered.
    assert "**Kindle Store categories (10 of 10)**" in md
    assert "**Books store categories (10 of 10)**" in md
    assert "10. **Books category 9**" in md
    assert "10. **Kindle category 9**" in md

    # 20 keywords in the ranking table, and the model's plan for them.
    assert "**Backend keywords — 20 ranked for ranking potential and low competition**" in md
    assert "| 20 |" in md
    assert "Lead with the lowest-competition phrase in the title." in md

    # Marketplace order and the model's interpretation.
    assert "**Marketplace ranking — where this book is most likely to perform**" in md
    assert "| 1 | Amazon UK (uk)" in md
    assert "Publish on Amazon UK first, then the US." in md

    # Ten premium titles, written from the measured keywords.
    assert "**Premium titles — 10 written from these keywords and this niche**" in md
    assert "| 10 | Grief Journal Title 9" in md
    assert "It matches the measured phrase exactly." in md

    # Reviews: raw findings, the verbatim quote, then the model's plan.
    assert "**How to beat the incumbent books — from their readers' reviews**" in md
    assert "too much filler and fluff" in md
    assert "> 5★ Loved it: The prompts helped me write again." in md
    assert "Cut the filler and add a sudden-loss section." in md

    # The PDF path carries the same sections.
    from app.pdf_export import model_to_blocks

    _, _, blocks = model_to_blocks(model.to_dict())
    text = " ".join(str(b.get("x", "")) for b in blocks if b.get("t") in ("p", "h2", "h3"))
    assert "Final Candidate Playbooks" in text
    assert "Final candidate 1 — grief journals for adult children" in text
    assert "Publish on Amazon UK first, then the US." in text
    table_rows = [
        row for b in blocks if b.get("t") == "table"
        for row in (b.get("rows") or [])
    ]
    assert any("Grief Journal Title 9" in str(r) for r in table_rows)


def test_dossier_cross_references_the_playbook_instead_of_repeating_it():
    from app.report_model import ReportModel, render_markdown

    sec = _section_one()
    opp = {
        "name": "Grief Journals",
        "candidate_id": "c1",
        "niche": "grief journals for adult children",
        "confidence": 0.8,
        "backend_research": {"keywords": sec["keywords"], "books": sec["categories"]["books"],
                             "kindle_store": sec["categories"]["kindle_store"]},
        "evidence_ids": [],
        "verification": {},
        "keywords": {},
        "angles": [],
        "risks": [],
    }
    model = ReportModel(
        session={"id": "s1", "name": "Run", "mode": "prompt"},
        executive_summary={},
        opportunities=[opp],
        finalist_sections=[sec],
        finalist_playbooks={"c1": _playbook(sec)},
    )
    md = render_markdown(model)
    assert "see Final candidate 1 below" in md
    # The old duplicate tables are gone from the dossier…
    assert "Backend keywords researched for this niche" not in md
    assert "Store categories researched for this niche" not in md
    # …and the full detail appears exactly once, in the playbook section.
    assert md.count("**Backend keywords — 20 ranked") == 1
    assert md.count("**Kindle Store categories (10 of 10)**") == 1


def test_report_states_absence_when_the_model_wrote_nothing():
    from app.report_model import ReportModel, render_markdown

    model = ReportModel(
        session={"id": "s1", "name": "Run", "mode": "prompt"},
        executive_summary={},
        finalist_sections=_sections()[:1],
        finalist_playbooks={},
    )
    md = render_markdown(model)
    assert "did not produce grounded titles" in md
    assert "did not write the differentiation plan" in md
    # The measured tables are unaffected by the model's absence.
    assert "**Backend keywords — 20 ranked" in md


# ------------------------------------------------------------ the dive itself
@pytest.mark.asyncio
async def test_dive_reads_live_reviews_while_mapping_books_categories(db):
    """Reviews come off the SAME product pages the category map opens."""
    from app.browser_store import BrowserEvidenceStore
    from app.evidence_capture import _extract_review_snippets
    from app.final_dive import FinalCandidateDive
    from app.marketplace import AMAZON_MARKETPLACES

    class _El:
        def __init__(self, text: str) -> None:
            self.text = text

        async def inner_text(self) -> str:
            return self.text

        async def get_attribute(self, _name: str):
            return None

    class _ReviewBlock:
        async def query_selector(self, sel: str):
            if "review-body" in sel:
                return _El("Half the pages were blank filler.")
            if "review-title" in sel:
                return _El("Needs substance")
            if "review-star-rating" in sel:
                return _El("2.0 out of 5 stars")
            return None

    class _Page:
        def __init__(self, url: str, *, product: bool = False) -> None:
            self.url = url
            self._product = product

        async def query_selector_all(self, sel: str):
            if "data-hook='review'" in sel:
                return [_ReviewBlock(), _ReviewBlock()] if self._product else []
            if "s-search-result" in sel:
                return [_Card()] if not self._product else []
            return []

        async def query_selector(self, sel: str):
            if "productTitle" in sel:
                return _El("A Competitor Grief Journal")
            if "wayfinding-breadcrumbs" in sel:
                return None
            return None

        async def evaluate(self, _js, arg=None):
            return []

    class _Card:
        async def get_attribute(self, name: str):
            return "0100000001" if name == "data-asin" else None

        async def query_selector(self, sel: str):
            if "h2" in sel:
                return _El("A Competitor Grief Journal")
            if "offscreen" in sel:
                return _El("$7.99")
            if "icon-alt" in sel:
                return _El("4.4 out of 5 stars")
            return _El("310")

    class _Browser:
        async def open_marketplace(self, mkt, path: str = "/"):
            return _Page(f"{mkt.base_url}{path}")

        async def navigate(self, page, url: str):
            if "/dp/" in url:
                return _Page(url, product=True)
            return _Page(url)

        async def close_page(self, page) -> None:
            return None

    # The extractor itself sees the review DOM.
    snippets = await _extract_review_snippets(_Page("https://x/dp/0100000001", product=True), 4)
    assert snippets
    assert snippets[0]["body"] == "Half the pages were blank filler."
    assert snippets[0]["rating"] == 2.0
    assert snippets[0]["asin"] == "0100000001"

    with db.tx() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO research_sessions (id, name, status, mode, "
            "created_at, updated_at, last_activity_at) VALUES ('s1', 'n', "
            "'running', 'prompt', '2025-01-01', '2025-01-01', '2025-01-01')"
        )
    store = BrowserEvidenceStore(db)
    res = await FinalCandidateDive(
        _Browser(), store, max_keywords=0, max_categories=10,
        time_budget_seconds=30,
    ).run(
        session_id="s1",
        marketplaces=[AMAZON_MARKETPLACES["uk"]],
        niche="grief journals",
        candidate_id="c1",
    )
    assert res.reviews, "reviews were not captured from the product pages"
    assert res.review_products_read >= 1
    assert res.review_evidence_id
    row = next(e for e in store.list("s1") if e.kind == "review_research")
    assert row.data["reviews"][0]["body"] == "Half the pages were blank filler."
    assert "niche" in row.data


# ------------------------------------------------------------- the runner
class _PlaybookModel:
    """Answers the playbook call from the digest; delegates everything else."""

    def __init__(self, base: Any = None, *, fail_on: tuple[str, ...] = ()) -> None:
        self.base = base
        self.fail_on = fail_on
        self.digests: list[dict[str, Any]] = []

    async def call(self, session_id: str, *, system: str, user: str, **kw: Any):
        from app.finalist_playbook import PLAYBOOK_SYSTEM
        from app.report_narrative import NARRATIVE_SYSTEM
        from tests.test_research_runner import FakeModelReply

        if system == PLAYBOOK_SYSTEM:
            body = user.split("facts you may use):", 1)[1]
            body = body.split("\n\nWrite this candidate's playbook", 1)[0]
            digest = json.loads(body)
            self.digests.append(digest)
            niche = digest["candidate"]["niche"]
            if niche in self.fail_on:
                raise RuntimeError("model down")
            kw = digest["keywords"][0]["keyword"]
            return FakeModelReply({
                "titles": [
                    {
                        "title": f"Grounded Title {i}",
                        "subtitle": f"Subtitle {i}",
                        "targets_keyword": kw,
                        "why_it_ranks": "It matches the measured phrase.",
                        "why_it_converts": "It names the reader.",
                    }
                    for i in range(10)
                ],
                "marketplace_plan": "Publish in the ranked order shown.",
                "category_plan": "Select into both catalogue trees.",
                "keyword_plan": "Lead with the first phrase.",
                "beating_incumbents": "Answer the top complaint.",
            })
        if system == NARRATIVE_SYSTEM:
            return FakeModelReply({})
        assert self.base is not None
        return await self.base.call(session_id, system=system, user=user, **kw)

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_runner_writes_one_playbook_per_finalist_and_isolates_failures(
    db, config, session
):
    from app.report_model import ReportModel, _dive_index
    from app.research_runner import ResearchRunner

    model = ReportModel(
        session={"id": session, "name": "Run", "mode": "prompt"},
        executive_summary={},
        # Both finalists have measured keywords — the failure below is the
        # model's, not a missing-research skip.
        finalist_sections=_sections(
            dive_by_niche=_dive_index(_dive_rows(NICHE) + _dive_rows(OTHER))
        ),
    )
    runner = ResearchRunner.__new__(ResearchRunner)  # loop under test only
    runner.model = _PlaybookModel(fail_on=(OTHER,))
    runner.events = _FakeEvents()
    runner._record_model_usage = lambda *a, **k: None  # type: ignore[method-assign]

    playbooks = await runner._finalist_playbooks(session, model)
    assert list(playbooks) == ["c1"]                    # c2 failed, c1 survived
    assert len(playbooks["c1"]["titles"]) == 10
    # The model only ever saw that candidate's measured data.
    assert [d["candidate"]["niche"] for d in runner.model.digests] == [
        "grief journals for adult children", OTHER,
    ]
    assert runner.model.digests[0]["keywords"]
    assert any(e["action"] == "finalist_playbook_written" for e in runner.events.rows)
    assert any(e["action"] == "finalist_playbook_unavailable" for e in runner.events.rows)

    # A candidate with no measured keyword is never asked for titles at all.
    empty = ReportModel(
        session={"id": session, "name": "Run", "mode": "prompt"},
        executive_summary={},
        finalist_sections=[s for s in _sections() if s["candidate_id"] == "c2"],
    )
    runner.model = _PlaybookModel()
    runner.events = _FakeEvents()
    assert await runner._finalist_playbooks(session, empty) == {}
    assert runner.model.digests == []
    assert runner.events.rows[0]["detail"]["reason"].startswith("no keyword")


class _FakeEvents:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def append(self, _session_id: str, **kw: Any) -> None:
        self.rows.append(kw)


@pytest.mark.asyncio
async def test_full_run_renders_a_complete_section_for_every_final_candidate(
    db, config, session, monkeypatch
):
    """End-to-end: live-measurement evidence -> sections -> model prose -> report."""
    from app.browser_store import BrowserEvidenceStore
    from app.final_dive import DiveResult
    from app.reports import ReportStore
    from tests.test_research_runner import (
        PHASE_OUTPUTS,
        ScriptedModel,
        make_runner,
    )

    store = BrowserEvidenceStore(db)

    class _StubDive:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def run(self, *, session_id, marketplaces, niche, candidate_id=""):
            ids: list[str] = []
            for row in _dive_rows(niche):
                rec = store.create(
                    session_id=session_id,
                    marketplace=row["marketplace"],
                    kind=row["kind"],
                    url=row["url"],
                    title=f"{row['kind']} for {niche[:60]}",
                    data=row["data"],
                )
                ids.append(str(rec.id))
            return DiveResult(
                candidate_id=candidate_id, niche=niche, marketplace="uk",
                evidence_ids=ids, keywords_measured=20,
            )

    monkeypatch.setattr("app.research_runner.FinalCandidateDive", _StubDive)

    runner = make_runner(
        db, config, _PlaybookModel(ScriptedModel(PHASE_OUTPUTS))
    )
    await runner.run(session)

    reps = ReportStore(db, config)
    final = [r for r in reps.list(session) if r.kind == "final"]
    assert final, "no final report persisted"
    md = reps.read_markdown(final[0])
    model_json = reps.read_model_json(final[0])
    assert model_json, "structured model was not persisted"

    sections = model_json["finalist_sections"]
    assert sections, "no final candidate sections were built"
    for sec in sections:
        assert len(sec["categories"]["kindle_store"]) == 10
        assert len(sec["categories"]["books"]) == 10
        assert len(sec["keywords"]) == 20
        assert sec["review_findings"]["reviews"]
        assert sec["marketplace_ranking"]

    playbooks = model_json["finalist_playbooks"]
    assert set(playbooks) == {s["candidate_id"] for s in sections}
    assert all(len(p["titles"]) == 10 for p in playbooks.values())

    assert "## Final candidate playbooks" in md
    assert f"### Final candidate 1 — {sections[0]['niche']}" in md
    assert "**Kindle Store categories (10 of 10)**" in md
    assert "**Backend keywords — 20 ranked for ranking potential and low competition**" in md
    assert "**Premium titles — 10 written from these keywords and this niche**" in md
    assert "Grounded Title 9" in md
    assert "Publish in the ranked order shown." in md
