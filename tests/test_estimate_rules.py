"""KDSpy Pro is an ESTIMATE, never a decision.

A third-party extension's rendered figures are unverified by construction,
so every place that used to let them count as evidence now refuses them:
the claim's quality class, the verification engine's coverage and
corroboration math, the supported-claim ratio, the evidence gate, and the
deterministic demand filter. Corroborated by a real page capture, the same
fact becomes ordinary evidence again — that is the "until proven otherwise".
"""
from __future__ import annotations

import pytest

from app.claims import (
    EQ_DIRECT,
    EQ_EXTENSION,
    EVIDENCE_QUALITY_WEIGHTS,
    ClaimStore,
    claim_is_estimate_backed,
    evidence_quality_from_kind,
    estimate_evidence_ids,
)
from app.filters import _demand_signal_count
from app.integrity import IntegrityAssessmentStore
from app.quality_gates import QualityGateRunner
from app.research_data import CandidateStore, OpportunityStore, VerificationStore
from app.sessions import SessionStore
from app.thresholds import EvidenceThresholds
from app.verification import VerificationEngine


def _recent_iso() -> str:
    """Fresh timestamp (evidence must be recent to look alive)."""
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()


def _evidence(n: int, marketplace: str = "us") -> list[dict]:
    return [
        {
            "id": f"ev{i}",
            "kind": "search_results",
            "marketplace": marketplace,
            "title": "search results",
            "url": f"https://www.amazon.com/s?k=grief&page={i}",
            "data": {"results": [{"asin": f"A{i}", "review_count": 40 + i}]},
            "captured_at": _recent_iso(),
        }
        for i in range(n)
    ]


def _supported_claims(db, session_id, cand_id, evidence_ids):
    store = ClaimStore(db)
    return [
        store.create(session_id, kind="demand",
                     statement="multiple competitors show steady review accumulation",
                     evidence_ids=evidence_ids, candidate_id=cand_id),
        store.create(session_id, kind="competition",
                     statement="incumbents cluster at generic positioning",
                     evidence_ids=evidence_ids[:1], candidate_id=cand_id),
        store.create(session_id, kind="market_gap",
                     statement="no incumbent offers estate-guidance structure",
                     evidence_ids=evidence_ids[:1], candidate_id=cand_id),
        store.create(session_id, kind="consumer_need",
                     statement="reviewers repeatedly request more structure",
                     evidence_ids=evidence_ids, candidate_id=cand_id),
    ]


@pytest.fixture()
def session_id(db, config) -> str:
    return SessionStore(db, config).create(
        mode="prompt", prompt="research grief journals for adults"
    ).id


def _kdspy(eid: str, url: str = "https://www.amazon.com/s?k=grief") -> dict:
    return {
        "id": eid,
        "kind": "kdspy_panel",
        "marketplace": "us",
        "title": "KDSpy panel",
        "url": url,
        "data": {"text_excerpt": "sales estimate 320/mo"},
        "captured_at": _recent_iso(),
    }


# ------------------------------------------------------------------- classes
def test_extension_evidence_is_the_estimate_class() -> None:
    assert evidence_quality_from_kind("kdspy_panel") == EQ_EXTENSION
    assert evidence_quality_from_kind("search_results") == EQ_DIRECT
    assert evidence_quality_from_kind("keyword_research") == EQ_DIRECT
    assert evidence_quality_from_kind("category_research") == EQ_DIRECT
    # An estimate can never outweigh an observation of the same fact.
    assert EVIDENCE_QUALITY_WEIGHTS[EQ_EXTENSION] < EVIDENCE_QUALITY_WEIGHTS[EQ_DIRECT]
    assert EVIDENCE_QUALITY_WEIGHTS[EQ_EXTENSION] < EVIDENCE_QUALITY_WEIGHTS["uncertain_observation"] + 0.2


def test_claim_quality_comes_from_the_real_evidence_kinds(db, session_id) -> None:
    store = ClaimStore(db)
    kinds = {"ev1": "search_results", "kd": "kdspy_panel"}

    only_kdspy = store.create(
        session_id, kind="demand", statement="the panel says demand is strong",
        evidence_ids=["kd"], evidence_kinds=kinds,
    )
    assert only_kdspy.quality == EQ_EXTENSION

    observed = store.create(
        session_id, kind="demand", statement="the search page shows demand",
        evidence_ids=["ev1", "kd"], evidence_kinds=kinds,
    )
    assert observed.quality == EQ_DIRECT

    assert claim_is_estimate_backed(only_kdspy, {"kd"}) is True
    assert claim_is_estimate_backed(observed, {"kd"}) is False
    assert claim_is_estimate_backed(observed, None) is False


def test_estimate_ids_are_derived_from_the_evidence_rows() -> None:
    ids = estimate_evidence_ids([
        {"id": "a", "kind": "kdspy_panel"},
        {"id": "b", "kind": "search_results"},
        type("Row", (), {"id": "c", "kind": "kdspy_panel"})(),
    ])
    assert ids == {"a", "c"}


# ----------------------------------------------------------------- verification
class TestVerificationIgnoresEstimates:
    def _engine(self, db) -> VerificationEngine:
        return VerificationEngine(
            EvidenceThresholds(), ClaimStore(db), VerificationStore(db),
            IntegrityAssessmentStore(db),
        )

    def test_estimates_alone_never_verify_a_candidate(self, db, session_id) -> None:
        cand = CandidateStore(db).create(
            session_id, niche="sudden-loss grief journals for adult children",
            marketplace="us",
        )
        store = ClaimStore(db)
        store.create(
            session_id, kind="demand",
            statement="the extension reports strong monthly sales",
            evidence_ids=["kd1", "kd2"], candidate_id=cand.id,
            evidence_kinds={"kd1": "kdspy_panel", "kd2": "kdspy_panel"},
        )
        store.create(
            session_id, kind="competition",
            statement="the extension reports low competition here",
            evidence_ids=["kd1"], candidate_id=cand.id,
            evidence_kinds={"kd1": "kdspy_panel"},
        )
        evidence = [_kdspy("kd1"), _kdspy("kd2", url="https://www.amazon.com/s?k=grief2")]
        rep = self._engine(db).verify(
            session_id, cand, evidence_records=evidence,
            estimate_ids={"kd1", "kd2"},
        )
        assert rep.verdict != "verified"
        assert rep.checks["evidence_records"] == 0
        assert rep.checks["estimate_records_ignored"] == 2
        assert any("extension estimate(s) not counted" in p for p in rep.problems)
        assert any("supported-claim ratio" in p for p in rep.problems)

    def test_page_evidence_behind_the_same_candidate_still_verifies(
        self, db, session_id
    ) -> None:
        cand = CandidateStore(db).create(
            session_id, niche="sudden-loss grief journals for adult children",
            marketplace="us",
        )
        _supported_claims(db, session_id, cand.id, ["ev1", "ev2"])
        rep = self._engine(db).verify(
            session_id, cand,
            evidence_records=[*_evidence(3), _kdspy("kd1")],
            estimate_ids={"kd1"},
        )
        assert rep.verdict == "verified"
        # The panel is still visible in the audit — just not counted.
        assert rep.checks["estimate_records_ignored"] == 1
        assert rep.checks["evidence_records"] == 3


# ---------------------------------------------------------------------- gates
def _gate_runner(db, session_id) -> QualityGateRunner:
    from app.browser_store import BrowserEvidenceStore

    claims = ClaimStore(db)
    candidates = CandidateStore(db)
    return QualityGateRunner(
        EvidenceThresholds(), claims, candidates, VerificationStore(db),
        OpportunityStore(db), IntegrityAssessmentStore(db),
        browser_evidence_count=12, observation_count=6,
        evidence_stores=(BrowserEvidenceStore(db),),
    )


def test_evidence_gate_refuses_estimates_as_proof(db, session_id) -> None:
    from app.browser_store import BrowserEvidenceStore

    store = BrowserEvidenceStore(db)
    ids = []
    for i in range(3):
        row = store.create(
            session_id=session_id, marketplace="us", kind="kdspy_panel",
            url=f"https://www.amazon.com/s?k=grief{i}", title="KDSpy panel",
            data={"text_excerpt": "estimate"},
        )
        ids.append(str(row.id))
    ClaimStore(db).create(
        session_id, kind="demand", statement="the panel says demand is huge",
        evidence_ids=ids,
        evidence_kinds={i: "kdspy_panel" for i in ids},
    )
    runner = _gate_runner(db, session_id)
    gate = runner._evidence_gate(session_id)
    assert gate.passed is False
    assert gate.metrics["cited_evidence"] == 0
    assert gate.metrics["estimate_evidence_ignored"] == 3
    assert "KDSpy estimate(s)" in gate.reason


def test_supported_claims_gate_counts_estimate_only_claims_as_unsupported(
    db, session_id
) -> None:
    from app.browser_store import BrowserEvidenceStore

    store = BrowserEvidenceStore(db)
    kd = store.create(
        session_id=session_id, marketplace="us", kind="kdspy_panel",
        url="https://www.amazon.com/s?k=grief", title="KDSpy panel",
        data={"text_excerpt": "estimate"},
    )
    ev = store.create(
        session_id=session_id, marketplace="us", kind="search_results",
        url="https://www.amazon.com/s?k=grief&page=2", title="search results",
        data={"results": []},
    )
    claims = ClaimStore(db)
    claims.create(
        session_id, kind="demand", statement="the panel reports strong sales",
        evidence_ids=[str(kd.id)], evidence_kinds={str(kd.id): "kdspy_panel"},
    )
    claims.create(
        session_id, kind="competition", statement="the search page shows few rivals",
        evidence_ids=[str(ev.id)], evidence_kinds={str(ev.id): "search_results"},
    )
    gate = _gate_runner(db, session_id)._supported_claims_gate(session_id)
    assert gate.metrics["claims"] == 2
    assert gate.metrics["estimate_backed"] == 1
    assert gate.metrics["unsupported"] == 1
    assert "KDSpy estimates" in gate.reason


# --------------------------------------------------------------------- filters
def test_a_kdspy_panel_alone_is_not_a_demand_signal() -> None:
    panel = [{"kind": "kdspy_panel", "data": {"text_excerpt": "sales 500"}}]
    assert _demand_signal_count(panel) == 0
    # Corroborated by an observed page, the observation is what counts.
    observed = [{
        "kind": "search_results",
        "data": {"results": [{"asin": "A1", "review_count": 120}]},
    }]
    assert _demand_signal_count(observed) == 1
    assert _demand_signal_count(panel + observed) == 1


# --------------------------------------------------------------------- report
def test_report_states_the_estimate_policy(db, config, session_id) -> None:
    from app.browser_store import BrowserEvidenceStore
    from app.report_model import build_report_model, render_markdown
    from app.research_data import ObservationStore

    cands = CandidateStore(db)
    cands.create(session_id, niche="grief journals for adults", marketplace="us")
    cands.create(session_id, niche="90-day prayer journals for new believers")
    cands.create(session_id, niche="guided divorce recovery journals")
    model = build_report_model(
        session_id,
        sessions=SessionStore(db, config),
        candidates=cands,
        opportunities=OpportunityStore(db),
        verifications=VerificationStore(db),
        claims=ClaimStore(db),
        browser_evidence=BrowserEvidenceStore(db),
        observations=ObservationStore(db),
        assessments=IntegrityAssessmentStore(db),
    )
    policy = model.quality_statement["estimate_policy"]
    assert "estimate" in policy.lower()
    assert "never count as evidence on their own" in policy
    md = render_markdown(model)
    assert "Estimates vs observations." in md
