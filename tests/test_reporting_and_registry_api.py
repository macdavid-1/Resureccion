"""Tests for report rendering and the methodology API endpoint.

Uses the structured report pipeline (model → markdown), which supersedes
the legacy markdown-only renderer.
"""
from __future__ import annotations

import pytest

from app.browser_store import BrowserEvidenceStore
from app.claims import ClaimStore
from app.methodology import PHASE_ORDER
from app.report_model import build_report_model, render_markdown
from app.research_data import (
    CandidateStore,
    EvidenceStore,
    ObservationStore,
    OpportunityStore,
    VerificationStore,
)
from app.sessions import SessionStore


def _build(db, config, session, *, phase_outputs=None) -> str:
    model = build_report_model(
        session,
        sessions=SessionStore(db, config),
        candidates=CandidateStore(db),
        opportunities=OpportunityStore(db),
        verifications=VerificationStore(db),
        claims=ClaimStore(db),
        browser_evidence=BrowserEvidenceStore(db),
        observations=ObservationStore(db),
        phase_outputs=phase_outputs or {p: {"x": 1} for p in PHASE_ORDER},
    )
    return render_markdown(model)


@pytest.fixture()
def session(db, config):
    return SessionStore(db, config).create(
        mode="prompt", prompt="grief journals", objective="validated niches"
    ).id


def test_report_lists_opportunities_and_candidates(db, config, session):
    candidates = CandidateStore(db)
    opportunities = OpportunityStore(db)
    cand = candidates.create(session, niche="sudden-loss grief journals", marketplace="us")
    opportunities.create(
        session, candidate_id=cand.id, title="Suddenly Gone",
        niche="sudden-loss grief journals for adult children",
        marketplace="us", angle="the organized grief journal",
        keywords=["grief journal"], confidence=0.8,
        meta={"market_gap": "estate guidance", "verification_status": "pass"},
    )
    body = _build(db, config, session)
    assert "Suddenly Gone" in body
    assert "sudden-loss grief journals" in body
    assert "estate guidance" in body
    assert "## Methodology trace" in body
    assert "## Candidate ledger" in body
    for phase in PHASE_ORDER:
        assert "Phase" in body  # phase titles appear in the trace


def test_report_with_no_opportunities_explains(db, config, session):
    body = _build(db, config, session, phase_outputs={})
    assert "No opportunities passed adversarial verification" in body


def test_phantom_evidence_ids_never_reach_the_report(db, config, session):
    """The model sometimes echoes invented evidence ids (e.g.
    'evidence_space_facts_uk'). They must not be cited in the report: the
    owner would chase ids that do not exist and the validator would mark the
    whole report PROVISIONAL."""
    bev = BrowserEvidenceStore(db)
    ev = EvidenceStore(db)
    real = bev.create(
        session_id=session, marketplace="us", kind="product",
        url="https://www.amazon.com/dp/REAL00001",
        title="Real Book", data={"title": "Real Book", "asin": "REAL00001"},
    )
    candidates = CandidateStore(db)
    opportunities = OpportunityStore(db)
    cand = candidates.create(session, niche="fact books", marketplace="us")
    opportunities.create(
        session, candidate_id=cand.id, title="Fact Books",
        niche="pocket fact books for curious readers", marketplace="us",
        angle="the pocket reference", keywords=["fact book"], confidence=0.7,
        meta={"market_gap": "a real gap", "verification_status": "pass",
              "evidence_ids": [real.id, "evidence_space_facts_uk"]},
    )
    model = build_report_model(
        session,
        sessions=SessionStore(db, config),
        candidates=candidates,
        opportunities=opportunities,
        verifications=VerificationStore(db),
        claims=ClaimStore(db),
        browser_evidence=bev,
        evidence=ev,
        observations=ObservationStore(db),
        phase_outputs={p: {"x": 1} for p in PHASE_ORDER},
    )
    cited = model.opportunities[0]["evidence_ids"]
    assert real.id in cited, "the real evidence id must survive"
    assert "evidence_space_facts_uk" not in cited, "invented id leaked into the report"
    for row in model.adjacent_ledger:
        assert "evidence_space_facts_uk" not in row["evidence_ids"]


def test_quality_statement_rejection_reasons_use_recorded_decisions(db, config, session):
    """Rejection reasons must be the recorded filter/verification DECISION,
    not the candidate's creation hint."""
    from app.integrity import KIND_FILTER, IntegrityAssessmentStore

    candidates = CandidateStore(db)
    cand = candidates.create(session, niche="weak niche", marketplace="us",
                             rationale="creation-time demand hint")
    candidates.set_status(session, cand.id, "rejected")
    IntegrityAssessmentStore(db).record(
        session, kind=KIND_FILTER, subject_type="candidate", subject_id=cand.id,
        stage="insufficient_evidence", outcome="fail",
        reason="only 1 evidence record backs this candidate; 3 required",
        metrics={},
    )
    model = build_report_model(
        session,
        sessions=SessionStore(db, config),
        candidates=candidates,
        opportunities=OpportunityStore(db),
        verifications=VerificationStore(db),
        claims=ClaimStore(db),
        browser_evidence=BrowserEvidenceStore(db),
        observations=ObservationStore(db),
        assessments=IntegrityAssessmentStore(db),
        phase_outputs={p: {"x": 1} for p in PHASE_ORDER},
    )
    reasons = model.quality_statement["rejection_reasons"]
    assert reasons and reasons[0]["niche"] == "weak niche"
    assert "evidence record" in reasons[0]["rationale"]
    assert "creation-time demand hint" not in reasons[0]["rationale"]


def test_methodology_endpoint(client):
    headers = {"X-Auth-Token": client.post(
        "/api/auth/login", json={"username": "owner", "password": "hunter2"}
    ).json()["token"]}
    res = client.get("/api/methodology", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert "phases" in data or "methodology" in data
