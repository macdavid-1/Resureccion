"""Analyst narrative tests — model writes prose, backend owns the numbers.

Covers the three guarantees:
  1. the digest handed to the model IS the verified deterministic data,
  2. invented figures never survive grounding (retry → strip),
  3. any failure degrades to the deterministic report.
"""
from __future__ import annotations

import pytest

from app.report_model import ReportModel, render_markdown
from app.report_narrative import (
    NARRATIVE_SYSTEM,
    allowed_numbers,
    build_digest,
    ground_text,
    normalize_narrative,
    unknown_numbers,
)
from tests.test_research_runner import session  # noqa: F401 — pytest fixture reuse


class TestDigest:
    def test_digest_carries_the_final_accounting_and_records(self):
        model = {
            "executive_summary": {
                "candidates_considered": 24, "candidates_rejected": 18,
                "survived_verification": 6, "why": "find gaps",
                "what_was_researched": "fact books",
                "marketplaces_investigated": ["us", "uk"],
                "demand_verdicts": ["sustained demand"],
                "major_findings": ["DK dominates"],
            },
            "quality_statement": {
                "verified": 2, "rejected": 18,
                "rejection_reasons": [{"niche": "x", "rationale": "thin evidence"}],
                "remains_uncertain": ["niche y"],
                "certainty_policy": "evidence only",
            },
            "opportunities": [
                {"name": "Pocket Fact Books", "niche": "pocket fact books",
                 "market_gap": "no UK price anchor", "confidence": 0.71,
                 "titles": ["should be slimmed"],  # not in _KEEP_OPP
                 "demand": {"verdict": "sustained demand"},
                 "verification": {"status": "verified"}},
            ],
            "competitors": [{"title": "Ocean: Facts", "price": "£5.99",
                             "internal_secret": "drop me"}],
            "consumer_intelligence": {"themes": {"complaints": ["too long"]}},
            "candidate_ledger": [{"niche": "dead niche", "status": "rejected"}],
            "headroom_ranking": [], "adjacent_ledger": [],
            "evidence_appendix": [{"id": "abc123def456", "kind": "product_page"}],
        }
        d = build_digest(model)
        acc = d["accounting"]
        assert acc["candidates_considered"] == 24
        assert acc["candidates_rejected"] == 18
        assert acc["survived_verification"] == 6
        assert acc["opportunities"] == 1
        assert acc["rejection_reasons"][0]["rationale"] == "thin evidence"
        assert d["opportunities"][0]["demand"]["verdict"] == "sustained demand"
        assert "titles" not in d["opportunities"][0], "non-selected keys must be dropped"
        assert "internal_secret" not in d["competitors"][0]
        assert d["evidence_appendix"][0]["id"] == "abc123def456"


class TestGrounding:
    def test_data_numbers_are_allowed_and_invented_ones_are_not(self):
        allowed = allowed_numbers('{"price": "£5.99", "review_count": 86, "rank": 31784}')
        assert unknown_numbers("priced at £5.99 with 86 reviews", allowed) == []
        assert unknown_numbers("rank 31,784 today", allowed) == []  # comma form
        assert unknown_numbers("48,210 sellers", allowed) == ["48,210"]
        assert unknown_numbers("a 98.7% share", allowed) == ["98.7"]

    def test_small_structural_integers_and_years_are_benign(self):
        allowed = allowed_numbers("{}")
        assert unknown_numbers("the top 10 results in phase 3", allowed) == []
        assert unknown_numbers("since 2024 and next year 2027", allowed) == []
        assert unknown_numbers("but 55,000 copies", allowed) == ["55,000"]

    def test_ground_text_drops_only_the_offending_sentence(self):
        allowed = allowed_numbers('{"n": 42}')
        text = (
            "Demand looks solid at 42 reviews. "
            "The niche also sells 913,000 copies a month. "
            "Overall the gap is real."
        )
        clean, removed = ground_text(text, allowed)
        assert "913,000" in removed
        assert "913,000" not in clean
        assert "42 reviews" in clean and "the gap is real" in clean

    def test_ground_text_drops_offending_bullets_and_keeps_clean_ones(self):
        allowed = allowed_numbers('{"n": 7}')
        text = "- seven is fine at 7\n- invented at 64,000\n- another clean one"
        clean, removed = ground_text(text, allowed)
        assert removed == ["64,000"]
        assert "64,000" not in clean
        assert "another clean one" in clean


class TestNormalize:
    def test_normalize_requires_sections_and_lowercases_niche_keys(self):
        allowed = allowed_numbers('{"n": 3}')
        narrative, violations = normalize_narrative({
            "overview": "# Heading not allowed\nStrong demand at 3 signals.",
            "opportunity_analyses": {
                "Pocket Fact Books": "Reader-first gap.",
                "  ": "blank key dropped",
            },
            "risks": ["not a string"],
            "extra": "ignored",
        }, allowed=allowed)
        assert not violations
        assert narrative["overview"].startswith("Strong demand")
        assert "# Heading" not in narrative["overview"]
        assert narrative["opportunity_analyses"]["pocket fact books"] == "Reader-first gap."
        assert "risks" not in narrative, "non-string section must be dropped"

    def test_normalize_reports_violations_it_removed(self):
        allowed = allowed_numbers('{"n": 3}')
        narrative, violations = normalize_narrative({
            "overview": "Sales hit 712,445 units.",
            "opportunity_analyses": {},
            "risks": "Fine print without figures.",
        }, allowed=allowed)
        assert any("712,445" in v for v in violations)
        assert "712,445" not in narrative.get("overview", "")
        assert narrative["risks"], "clean sections survive"

    def test_non_dict_reply_yields_empty_narrative(self):
        narrative, violations = normalize_narrative("garbage", allowed=set())
        assert narrative == {} and violations


class TestRendering:
    def test_narrative_sections_render_in_the_report(self):
        model = ReportModel(
            session={"id": "s", "name": "N", "mode": "prompt",
                     "marketplaces_requested": ["us"], "objective": "o",
                     "prompt": "p"},
            executive_summary={"what_was_researched": "x", "why": "y",
                               "marketplaces_investigated": ["us"],
                               "methodology": "m", "candidates_considered": 3,
                               "candidates_rejected": 1, "survived_verification": 2,
                               "demand_verdicts": [], "major_findings": []},
            opportunities=[{
                "name": "Pocket Facts", "niche": "pocket fact books",
                "parent_market": "fact books", "target_reader": "commuters",
                "reader_problem": "bored commutes", "marketplaces": ["uk"],
                "market_gap": "no pocket option", "differentiation": "visual",
                "positioning": "", "risks": [], "confidence": 0.7,
                "verification": {"status": "verified", "verdict": "verified",
                                 "confidence": 0.8},
                "niching_chain": [], "demand": {}, "evidence_ids": [],
            }],
            narrative={
                "overview": "The session points to pocket fact books.\nInference — UK pricing helps.",
                "opportunity_analyses": {
                    "pocket fact books": "Commuters lack a compact option.",
                },
                "risks": "Ratings were not measured beyond the captured set.",
            },
        )
        body = render_markdown(model)
        assert "## Analyst's overview" in body
        assert "Inference — UK pricing helps." in body
        assert "**Analyst's take:**" in body
        assert "Commuters lack a compact option." in body
        assert "## Risks, caveats and open questions" in body
        assert "Ratings were not measured" in body

    def test_model_json_round_trips_the_narrative(self):
        m = ReportModel(narrative={"overview": "text"})
        data = m.to_dict()
        assert data["narrative"]["overview"] == "text"
        again = ReportModel.from_dict(data)
        assert again.narrative == {"overview": "text"}


class _Reply:
    def __init__(self, content):
        self.content = content


class _SequenceModel:
    """Returns scripted replies only for the NARRATIVE prompt; phase prompts
    raise (like ScriptedModel) so the runner's fallback path is exercised."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.narrative_calls: list[str] = []

    async def call(self, session_id, *, system, user, **kw):
        assert system == NARRATIVE_SYSTEM or "market-intelligence analyst" in system
        self.narrative_calls.append(user)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return _Reply(reply)


class _RaisingModel:
    async def call(self, *a, **kw):
        raise AssertionError("model called with unknown phase prompt")


def _mini_runner(db, config, model):
    from tests.test_research_runner import make_runner

    return make_runner(db, config, model)


class TestRunnerNarrative:
    @pytest.mark.asyncio
    async def test_narrative_lands_when_the_model_answers(self, db, config, session):
        model = _SequenceModel([{
            "overview": "Demand is sustained across the 5 researched candidates.",
            "opportunity_analyses": {"some niche": "Why it works."},
            "risks": "Risks stay modest.",
        }])
        runner = _mini_runner(db, config, model)
        report = ReportModel(
            session={"id": session}, executive_summary={"candidates_considered": 5},
        )
        narrative = await runner._analyst_narrative(session, report)
        assert "Demand is sustained" in narrative["overview"]
        assert "5 researched" in narrative["overview"], "data numbers must survive grounding"
        assert narrative["opportunity_analyses"]["some niche"]
        assert len(model.narrative_calls) == 1

    @pytest.mark.asyncio
    async def test_invented_figures_are_corrected_then_stripped(self, db, config, session):
        model = _SequenceModel([
            {   # attempt 1: one invented figure
                "overview": "The category sells 913,000 copies. Demand is real.",
                "opportunity_analyses": {},
                "risks": "No invented numbers here.",
            },
            {   # attempt 2: corrected
                "overview": "The category sells well. Demand is real.",
                "opportunity_analyses": {},
                "risks": "No invented numbers here.",
            },
        ])
        runner = _mini_runner(db, config, model)
        narrative = await runner._analyst_narrative(
            session, ReportModel(session={"id": session})
        )
        assert len(model.narrative_calls) == 2, "violations trigger one retry"
        assert "913,000" not in narrative["overview"]
        assert "sells well" in narrative["overview"], "the corrected reply is used"

    @pytest.mark.asyncio
    async def test_model_failure_degrades_to_deterministic_report(
        self, db, config, session
    ):
        runner = _mini_runner(db, config, _RaisingModel())
        narrative = await runner._analyst_narrative(
            session, ReportModel(session={"id": session})
        )
        assert narrative == {}
        from app.events import EventLog

        actions = [e.action for e in EventLog(db).tail(session)]
        assert "report_narrative_unavailable" in actions, actions
