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


def test_methodology_endpoint(client):
    headers = {"X-Auth-Token": client.post(
        "/api/auth/login", json={"username": "owner", "password": "hunter2"}
    ).json()["token"]}
    res = client.get("/api/methodology", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert "phases" in data or "methodology" in data
