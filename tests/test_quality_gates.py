"""Tests for the 7 research quality gates and adversarial report validation."""
from __future__ import annotations

import pytest

from app.claims import ClaimStore
from app.integrity import IntegrityAssessmentStore
from app.quality_gates import QualityGateRunner
from app.report_validation import ReportValidator
from app.research_data import (
    CandidateStore,
    OpportunityStore,
    VerificationStore,
)
from app.sessions import SessionStore
from app.thresholds import EvidenceThresholds


def _seed(db, config, session_id, *, with_opportunity: bool = True,
          verified: bool = True) -> str:
    """Seed a session with a minimum viable evidence chain."""
    claims = ClaimStore(db)
    candidates = CandidateStore(db)
    c1 = candidates.create(session_id, niche="sudden-loss grief journals for adults",
                           marketplace="us")
    c2 = candidates.create(session_id, niche="90-day prayer journals for new believers")
    c3 = candidates.create(session_id, niche="guided divorce recovery journals for men")
    claims.create(session_id, kind="demand",
                  statement="multiple competitors show steady review accumulation",
                  evidence_ids=["ev1"], candidate_id=c1.id)
    claims.create(session_id, kind="competition",
                  statement="incumbents cluster at generic positioning",
                  evidence_ids=["ev2"], candidate_id=c1.id)
    if verified:
        VerificationStore(db).create(session_id, candidate_id=c1.id)
        VerificationStore(db).complete(
            session_id,
            VerificationStore(db).for_candidate(session_id, c1.id)[0].id,
            status="passed", verdict="verified", confidence=0.8,
        )
        # Mirror the pipeline: verification apply() moves candidate status.
        candidates.set_status(session_id, c1.id, "verified")
    if with_opportunity:
        OpportunityStore(db).create(
            session_id, candidate_id=c1.id,
            title="Suddenly Gone", niche="sudden-loss grief journals for adults",
            marketplace="us", angle="the organized grief journal",
            keywords=["grief journal"], confidence=0.7,
            meta={
                "parent_market": "grief journals", "target_reader": "adult children",
                "reader_problem": "overwhelm", "market_gap": "estate guidance",
                "differentiation": "estate checklist", "risks": ["narrow"],
                "evidence_ids": ["ev1"], "verification_status": "verified",
            },
        )
    return c1.id


def _gate_runner(db, session_id, thresholds=None, *, be_count=12, obs_count=6):
    return QualityGateRunner(
        thresholds or EvidenceThresholds(),
        ClaimStore(db),
        CandidateStore(db),
        VerificationStore(db),
        OpportunityStore(db),
        IntegrityAssessmentStore(db),
        browser_evidence_count=be_count,
        observation_count=obs_count,
    )


@pytest.fixture()
def sid(db, config):
    return SessionStore(db, config).create(
        mode="prompt", prompt="research grief journals"
    ).id


class TestQualityGates:
    def test_full_pass_session(self, db, config, sid):
        _seed(db, config, sid)
        report = _gate_runner(db, sid).run(sid)
        cand_gate = next(g for g in report.results if g.name == "candidate_gate")
        assert cand_gate.passed, cand_gate.reason
        assert report.passed, report.failed_blocking
        # All 8 gates ran and were recorded.
        names = {g.name for g in report.results}
        assert len(names) == 8
        audits = IntegrityAssessmentStore(db).list(sid, kind="quality_gate")
        assert len(audits) == 8

    def test_observation_floor_blocks_thin_sessions(self, db, config, sid):
        _seed(db, config, sid)
        report = _gate_runner(db, sid, be_count=2, obs_count=0).run(sid)
        assert not report.passed
        assert "observation_gate" in report.failed_blocking

    def test_unverified_opportunity_blocks(self, db, config, sid):
        _seed(db, config, sid, verified=False)
        report = _gate_runner(db, sid).run(sid)
        assert not report.passed
        assert "verification_coverage_gate" in report.failed_blocking

    def test_no_claims_blocks(self, db, config, sid):
        _seed(db, config, sid)
        # Wipe claims: session "reasoned" without registering evidence chains.
        with db.tx() as conn:
            conn.execute("DELETE FROM claims WHERE session_id = ?", (sid,))
        report = _gate_runner(db, sid).run(sid)
        assert not report.passed
        assert "supported_claims_gate" in report.failed_blocking

    def test_candidate_pipeline_floor(self, db, config, sid):
        _seed(db, config, sid)
        # Keep only the verified candidate; FK-protected by deleting the
        # opportunity first, then the other candidate rows.
        with db.tx() as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("DELETE FROM opportunities WHERE session_id = ?", (sid,))
            conn.execute(
                "DELETE FROM candidates WHERE session_id = ? AND status != 'verified'",
                (sid,))
            conn.execute("PRAGMA foreign_keys = ON")
        report = _gate_runner(db, sid).run(sid)
        assert "candidate_gate" in report.failed_blocking


class TestReportValidation:
    def test_validated_session_produces_final_report(self, db, config, sid):
        _seed(db, config, sid)
        validator = ReportValidator(
            ClaimStore(db), OpportunityStore(db), VerificationStore(db),
            _gate_runner(db, sid), IntegrityAssessmentStore(db),
            candidates=CandidateStore(db),
        )
        result = validator.validate(sid, report_body="# Report\n\nAll good.")
        assert result.passed, result.failures
        assert result.mode == "final"
        # Persisted as a durable report_validation assessment.
        audits = IntegrityAssessmentStore(db).list(sid, kind="report_validation")
        assert audits and audits[0].outcome == "pass"

    def test_unverified_opportunity_forces_provisional(self, db, config, sid):
        _seed(db, config, sid, verified=False)
        validator = ReportValidator(
            ClaimStore(db), OpportunityStore(db), VerificationStore(db),
            _gate_runner(db, sid), IntegrityAssessmentStore(db),
            candidates=CandidateStore(db),
        )
        result = validator.validate(sid, report_body="# Report")
        assert not result.passed
        assert result.mode == "provisional"
        assert any("no verification record" in f for f in result.failures)

    def test_missing_chain_of_custody_flagged(self, db, config, sid):
        _seed(db, config, sid)
        # Strip evidence citations from claims and opportunity meta.
        with db.tx() as conn:
            conn.execute("UPDATE claims SET evidence_ids = '[]' WHERE session_id = ?", (sid,))
            conn.execute(
                "UPDATE opportunities SET meta = json_set(meta, '$.evidence_ids', json_array()) "
                "WHERE session_id = ?", (sid,))
        validator = ReportValidator(
            ClaimStore(db), OpportunityStore(db), VerificationStore(db),
            _gate_runner(db, sid), IntegrityAssessmentStore(db),
            candidates=CandidateStore(db),
        )
        result = validator.validate(sid, report_body="# Report")
        assert not result.passed
        assert any("chain-of-custody" in f for f in result.failures)

    def test_redaction_scan_catches_leaks(self, db, config, sid):
        _seed(db, config, sid)
        validator = ReportValidator(
            ClaimStore(db), OpportunityStore(db), VerificationStore(db),
            _gate_runner(db, sid), IntegrityAssessmentStore(db),
            candidates=CandidateStore(db),
        )
        leaky = "# Report\n\nauthorization: Bearer sk-live-abc123\ncookie: session=xyz"
        result = validator.validate(sid, report_body=leaky)
        assert not result.passed
        assert any("redaction scan" in f for f in result.failures)
