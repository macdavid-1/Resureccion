"""Screening: suggestions are filtered before anything is researched.

The dive must never pour the suggestion stream into the report. Roots
("journal for women"), noise and phrases that drifted away from the candidate
are refused with a reason; the survivors are measured one by one and refused
again if Amazon prints too much competition. The frontier applies the same
screen before the model sees the list, and the prompt says plainly that the
list is a starting point, not a boundary.
"""
from __future__ import annotations

import pytest

from app.final_dive import FinalCandidateDive
from app.frontier import filter_demand_terms, screen_phrase, topical_tokens
from app.marketplace import AMAZON_MARKETPLACES
from tests.test_final_dive import _Card, _DiveBrowser, _Page, _seed


# ------------------------------------------------------------------ screening
def test_the_screen_separates_specific_phrases_from_roots_and_drift() -> None:
    topics = topical_tokens("grief journals for adult children")

    assert screen_phrase("grief journals for adult children", topics) == (True, "")
    assert screen_phrase("grief journals for adult children", topics)[0]
    # Roots: format/audience words with no subject at all.
    assert screen_phrase("journal for women", topics) == (False, "too_generic")
    assert screen_phrase("journal", topics) == (False, "too_broad")
    # Drift: specific, but not this candidate's subject.
    assert screen_phrase("space facts for kids", topics) == (False, "off_niche")
    # Marketplace noise is never a keyword.
    assert screen_phrase("amazon kindle unlimited", topics) == (False, "noise")
    # A truncated prefix is an unfinished query, not demand language.
    assert screen_phrase("grief journals for", topics) == (False, "incomplete")


def test_filter_keeps_the_specific_ones_and_counts_the_rest() -> None:
    kept, rejected = filter_demand_terms(
        [
            "grief journal for adult children",
            "journal for women",
            "space facts for kids",
            "journal",
        ],
        ["grief", "journals"],
        limit=10,
    )
    assert kept == ["grief journal for adult children"]
    assert rejected == {"too_generic": 1, "off_niche": 1, "too_broad": 1}


def test_the_frontier_prompt_says_the_list_is_a_starting_point() -> None:
    from app.methodology import PhaseTiming
    from app.prompts import build_user_prompt

    text = build_user_prompt(
        phase="opportunity_discovery",
        mode="prompt",
        objective="",
        prompt_text="grief journals for adult children",
        marketplaces=["us"],
        marketplace_mode="explicit",
        evidence_digest=[],
        candidates_digest=[],
        prior_phase_outputs=None,
        timing=PhaseTiming(phase="opportunity_discovery", started_at="now"),
        frontier_terms=["grief journal for adult children"],
    )
    assert "STARTING POINTS, not your boundary" in text
    assert "sample, not a fence" in text
    assert "propose niches of your own" in text


# --------------------------------------------------- measurement accept/reject
class _CompetitionBrowser(_DiveBrowser):
    """Every "<phrase> workbook" search prints millions of results."""

    async def navigate(self, page, url: str):
        # A search URL is a NEW results page: reuse the fixture's search page so
        # each keyword gets its own printed total.
        if "/s?" in url:
            from urllib.parse import urlparse

            parsed = urlparse(url)
            return self._search(url, parsed.path + "?" + parsed.query)
        return await super().navigate(page, url)

    def _search(self, url: str, path: str):
        from urllib.parse import parse_qs, urlparse

        query = parse_qs(urlparse(path).query).get("k", [""])[0]
        total = 7_000_000 if "workbook" in query else 3_000
        cards = [_Card(f"{i:09d}", f"Book {i}", 10) for i in range(3)]
        return _Page(
            url,
            body=f"1-16 of over {total:,} results for {query}",
            cards=cards,
        )


@pytest.mark.asyncio
async def test_high_competition_phrases_are_rejected_not_ranked_last(db, config):
    from app.browser_store import BrowserEvidenceStore

    _seed(db)
    store = BrowserEvidenceStore(db)
    res = await FinalCandidateDive(
        _CompetitionBrowser(), store,
        max_keywords=3, max_categories=0, autocomplete_queries=12,
        products_per_store=1, time_budget_seconds=60,
    ).run(
        session_id="s1",
        marketplaces=[AMAZON_MARKETPLACES["us"]],
        niche="grief journals for adult children",
        candidate_id="c1",
    )

    assert res.error is None, res.error
    assert res.keywords_measured == 3, "the loop must keep going past rejects"
    assert all(k["total_results"] == 3_000 for k in res.keywords)
    assert not any("workbook" in k["keyword"] for k in res.keywords)

    refused = [r for r in res.rejected if r["reason"] == "too_much_competition"]
    assert refused, "a 7,000,000-result phrase must be refused"
    assert all(r["stage"] == "measured" for r in refused)
    assert all(r["total_results"] == 7_000_000 for r in refused)
    assert res.competition_ceiling == 25_000

    # The refusal is durable evidence, not just an in-memory detail.
    row = next(e for e in store.list("s1") if e.kind == "keyword_research")
    assert row.data["competition_ceiling"] == 25_000
    assert any(
        r["reason"] == "too_much_competition" for r in row.data["rejected"]
    )


@pytest.mark.asyncio
async def test_the_dive_deepens_from_the_keywords_that_measured_well(db, config):
    """A shallow sweep would stop at the niche's own wording. Once phrases are
    accepted, one bounded second sweep mines FROM them (buyers complete
    "grief journals for adult children" into phrasings the label never holds)."""
    from app.browser_store import BrowserEvidenceStore

    _seed(db)

    class _Sparse(_DiveBrowser):
        def __init__(self) -> None:
            super().__init__()
            self.asked: list[str] = []

        async def open_marketplace(self, mkt, path: str = "/"):
            page = await super().open_marketplace(mkt, path)
            original = page.evaluate

            async def evaluate(js, arg=None):
                prefix = str(arg[-1] if isinstance(arg, (list, tuple)) and arg else "")
                self.asked.append(prefix)
                if " " in prefix and len(prefix.split()) == 2:
                    # Letter frontier ("seed a") yields nothing; only whole
                    # phrases complete.
                    return []
                return await original(js, arg)

            page.evaluate = evaluate
            return page

    res = await FinalCandidateDive(
        _Sparse(), BrowserEvidenceStore(db),
        max_keywords=20, max_categories=0, autocomplete_queries=40,
        time_budget_seconds=60,
    ).run(
        session_id="s1",
        marketplaces=[AMAZON_MARKETPLACES["us"]],
        niche="grief journals for adult children",
        candidate_id="c1",
    )

    assert res.error is None, res.error
    assert res.keywords_measured >= 12, "the second sweep never ran"
    keywords = [k["keyword"] for k in res.keywords]
    assert len(keywords) == len(set(keywords)), "no phrase may be measured twice"


@pytest.mark.asyncio
async def test_a_pool_of_only_losing_phrases_is_reported_as_such(db, config):
    """When every measured phrase is over the ceiling, the honest answer is
    'nothing here can rank' — with the numbers attached, never a filled table."""
    from app.browser_store import BrowserEvidenceStore

    _seed(db)

    class _AllHuge(_CompetitionBrowser):
        def _search(self, url: str, path: str):
            from urllib.parse import parse_qs, urlparse

            query = parse_qs(urlparse(path).query).get("k", [""])[0]
            cards = [_Card(f"{i:09d}", f"Book {i}", 10) for i in range(3)]
            return _Page(
                url,
                body=f"1-16 of over 7,000,000 results for {query}",
                cards=cards,
            )

    store = BrowserEvidenceStore(db)
    res = await FinalCandidateDive(
        _AllHuge(), store,
        max_keywords=3, max_categories=0, autocomplete_queries=8,
        time_budget_seconds=60,
    ).run(
        session_id="s1",
        marketplaces=[AMAZON_MARKETPLACES["us"]],
        niche="grief journals for adult children",
        candidate_id="c1",
    )

    assert res.error is None
    assert res.keywords_measured == 0
    assert res.done, "a clean refusal is a finished dive, not a failure"
    measured_refusals = [
        r for r in res.rejected if r.get("stage") == "measured"
    ]
    assert measured_refusals
    assert {r["reason"] for r in measured_refusals} == {"too_much_competition"}
    row = next(e for e in store.list("s1") if e.kind == "keyword_research")
    assert row.data["measured"] == 0
    assert row.data["rejected"]
