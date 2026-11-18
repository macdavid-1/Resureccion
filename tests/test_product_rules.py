"""The owner's hard rules: no journals/logbooks, no generic niches.

They are enforced in three places on purpose — at candidate creation (so no
research time is spent on a book nobody may write), in the deterministic filter
(so an inherited or hand-made row cannot slip through), and in the prompt (so
the model does not waste a phase proposing one).
"""
from __future__ import annotations

import pytest

from app.product_rules import banned_product_term, generic_niche_reason, screen_niche
from tests.test_research_runner import PHASE_OUTPUTS  # noqa: F401 — fixture reuse


@pytest.mark.parametrize(
    "niche",
    [
        "grief journals for adult children",
        "5-minute journaling prompts for anxious moms",
        "gratitude log book for men in recovery",
        "daily diary for new mothers",
        "teen notebook with prompts",
        "logbook for amateur astronomers",
    ],
)
def test_journal_and_logbook_products_are_refused(niche: str) -> None:
    reason = screen_niche(niche)
    assert reason, f"{niche!r} must be refused"
    assert "journal/logbook" in reason
    assert banned_product_term(niche)


@pytest.mark.parametrize(
    "niche",
    [
        "self help",
        "books for women",
        "mindfulness",
        "parenting for beginners",
        "finance",
    ],
)
def test_generic_markets_are_refused(niche: str) -> None:
    reason = screen_niche(niche)
    assert reason, f"{niche!r} must be refused"
    assert "journal/logbook" not in reason, "this is the generic rule, not the product rule"


@pytest.mark.parametrize(
    "niche",
    [
        "sudden-loss grief memoirs for adults settling a parent's estate",
        "first-year sobriety handbooks for men leaving residential treatment",
        "coastal marathon training plans for runners returning from injury",
        "perimenopause sleep guide for shift nurses",
    ],
)
def test_specific_non_journal_niches_pass(niche: str) -> None:
    assert screen_niche(niche) == ""
    assert generic_niche_reason(niche) == ""


def test_the_filter_rejects_a_journal_candidate_with_its_reason(db, config) -> None:
    from app.filters import CandidateFilterEngine
    from app.integrity import IntegrityAssessmentStore
    from app.research_data import CandidateStore
    from app.sessions import SessionStore
    from app.thresholds import EvidenceThresholds

    sid = SessionStore(db, config).create(
        mode="prompt", prompt="find niches", objective="o"
    ).id
    cand = CandidateStore(db).create(sid, niche="90-day prayer journals for new believers")
    rep = CandidateFilterEngine(
        EvidenceThresholds(), IntegrityAssessmentStore(db)
    ).evaluate(sid, cand, evidence_records=[], claims=[], existing_niches=[])
    assert rep.verdict == "reject"
    assert any(f.name == "excluded_niche" for f in rep.failures)
    # The refusal is auditable, not silent.
    audits = IntegrityAssessmentStore(db).list(sid, kind="filter")
    assert any(a.stage == "excluded_niche" and a.outcome == "fail" for a in audits)


@pytest.mark.asyncio
async def test_the_runner_never_creates_a_refused_candidate(db, config):
    from app.events import EventLog
    from app.research_data import CandidateStore
    from app.sessions import SessionStore
    from tests.test_research_runner import PHASE_OUTPUTS, ScriptedModel, make_runner

    sid = SessionStore(db, config).create(
        mode="prompt", prompt="find niches", objective="o"
    ).id
    runner = make_runner(db, config, ScriptedModel(PHASE_OUTPUTS))
    runner._apply_phase_effects(
        sid, "aggressive_niching",
        {"niches": [
            {"niche": "grief journals for adult children", "marketplace": "us"},
            {"niche": "self help books for women", "marketplace": "us"},
            {"niche": "sudden-loss grief memoirs for adult children",
             "marketplace": "us", "demand_hints": "thin competition"},
        ]},
    )
    created = [c.niche for c in CandidateStore(db).list(sid)]
    assert created == ["sudden-loss grief memoirs for adult children"]
    refused = [
        e for e in EventLog(db).tail(sid) if e.action == "niche_refused_by_hard_rule"
    ]
    assert len(refused) == 2
    assert all(e.detail.get("reason") for e in refused)


def test_the_model_cannot_add_a_journal_candidate(db, config):
    from app.browser_store import BrowserEvidenceStore
    from app.claims import ClaimStore
    from app.record_edits import RecordEditError, RecordEditor
    from app.research_data import CandidateStore, OpportunityStore
    from app.sessions import SessionStore

    sid = SessionStore(db, config).create(
        mode="prompt", prompt="find niches", objective="o"
    ).id
    editor = RecordEditor(
        candidates=CandidateStore(db),
        opportunities=OpportunityStore(db),
        claims=ClaimStore(db),
        browser_evidence=BrowserEvidenceStore(db),
    )
    with pytest.raises(RecordEditError, match="journal/logbook"):
        editor.apply(sid, "add_candidate", {"niche": "grief journals for adult children"})
    assert CandidateStore(db).list(sid) == []


def test_the_prompt_states_the_hard_rules() -> None:
    from app.methodology import P1_DISCOVERY
    from app.prompts import build_system_prompt

    text = build_system_prompt(P1_DISCOVERY)
    assert "HARD RULES" in text
    assert "journal, logbook, notebook or diary" in text
    assert "generic market" in text
