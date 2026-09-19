"""Tests for the research integrity layer: claims, thresholds, KDP risk,
verification engine, and the deterministic filters."""
from __future__ import annotations

import pytest

from app.claims import (
    CLAIM_UNSUPPORTED,
    ClaimError,
    ClaimStore,
    EQ_INFERENCE,
    EQ_REPEATED,
    compute_claim_confidence,
)
from app.filters import CandidateFilterEngine
from app.integrity import IntegrityAssessmentStore
from app.kdp_risk import record_screening, screen_candidate
from app.research_data import (
    CAND_REJECTED,
    CAND_UNDER_VERIFICATION,
    CAND_VERIFIED,
    CandidateStore,
    OpportunityStore,
    VerificationStore,
)
from app.sessions import SessionStore
from app.thresholds import EvidenceThresholds, get_thresholds, reset_thresholds
from app.verification import VerificationEngine, evidence_for_candidate


def _gate_runner(db, sid, *, be_count=12, obs_count=6):
    from app.quality_gates import QualityGateRunner

    return QualityGateRunner(
        EvidenceThresholds(), ClaimStore(db), CandidateStore(db),
        VerificationStore(db), OpportunityStore(db),
        IntegrityAssessmentStore(db), browser_evidence_count=be_count,
        observation_count=obs_count,
    )


@pytest.fixture()
def session_id(db, config) -> str:
    """Real research session row (FK target for claims/assessments)."""
    return SessionStore(db, config).create(
        mode="prompt", prompt="research grief journals for adults"
    ).id


# --------------------------------------------------------------------- claims
class TestClaims:
    def test_claim_with_evidence_is_supported_and_direct(self, db, session_id):
        store = ClaimStore(db)
        c = store.create(
            session_id, kind="demand",
            statement="review velocity is positive on the top 10 results",
            evidence_ids=["ev1", "ev2"],
        )
        assert c.status == "supported"
        assert c.quality == EQ_REPEATED  # 2+ evidence ids
        assert c.confidence > 0.9

    def test_claim_without_evidence_is_unsupported_inference(self, db, session_id):
        store = ClaimStore(db)
        c = store.create(session_id, kind="demand", statement="this market looks promising")
        assert c.status == CLAIM_UNSUPPORTED
        assert c.quality == EQ_INFERENCE

    def test_claim_confidence_capped_by_quality(self):
        conf = compute_claim_confidence(["a", "b", "c", "d"], EQ_INFERENCE)
        assert conf <= 0.4

    def test_invalid_kind_rejected(self, db, session_id):
        with pytest.raises(ClaimError):
            ClaimStore(db).create(session_id, kind="vibes", statement="nonsense assertion here")

    def test_set_verdict_and_status(self, db, session_id):
        store = ClaimStore(db)
        c = store.create(session_id, kind="risk", statement="KDP category may be policy-sensitive")
        c2 = store.set_verdict(session_id, c.id, "verified")
        assert c2.verdict == "verified"
        c3 = store.set_status(session_id, c.id, "contradicted")
        assert c3.status == "contradicted"
        with pytest.raises(ClaimError):
            store.set_verdict(session_id, c.id, "bogus")

    def test_for_evidence_finds_backlinks(self, db, session_id):
        store = ClaimStore(db)
        c = store.create(session_id, kind="demand",
                         statement="sustained demand across two marketplaces",
                         evidence_ids=["ev9"])
        found = store.for_evidence(session_id, "ev9")
        assert [x.id for x in found] == [c.id]

    def test_session_scoping(self, db, config):
        store = ClaimStore(db)
        s1 = SessionStore(db, config).create(mode="prompt", prompt="a")
        s2 = SessionStore(db, config).create(mode="prompt", prompt="b")
        store.create(s1.id, kind="demand", statement="claim for session one")
        store.create(s2.id, kind="demand", statement="claim for session two")
        assert len(store.list(s1.id)) == 1
        assert store.get(s2.id, store.list(s1.id)[0].id) is None


# ----------------------------------------------------------------- thresholds
class TestThresholds:
    def test_defaults_are_documented_and_strict(self):
        t = EvidenceThresholds()
        assert t.min_competitor_sample >= 6
        assert t.min_review_sample >= 8
        assert t.min_independent_sources >= 2
        assert t.min_verification_coverage == 1.0
        assert t.min_supported_claim_ratio >= 0.6
        d = t.describe()
        assert all("why" in v for v in d.values())

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("THRESH_MIN_COMPETITORS", "9")
        monkeypatch.setenv("THRESH_MIN_SOURCES", "3")
        t = EvidenceThresholds.from_env()
        assert t.min_competitor_sample == 9
        assert t.min_independent_sources == 3

    def test_singleton_reset(self, monkeypatch):
        reset_thresholds()
        t1 = get_thresholds()
        monkeypatch.setenv("THRESH_MIN_REVIEWS", "12")
        reset_thresholds()
        t2 = get_thresholds()
        assert t2.min_review_sample == 12
        reset_thresholds()  # don't leak env state into other tests


# ------------------------------------------------------------------ kdp risk
class TestKdpRisk:
    def test_clean_candidate_passes(self):
        cand = type("C", (), {"niche": "sudden-loss grief journals for adult children",
                              "rationale": "sustained autocomplete demand", "meta": {}})()
        r = screen_candidate(cand)
        assert r.clean and not r.blocking

    def test_trademarked_term_is_blocking(self):
        cand = type("C", (), {"niche": "harry potter inspired reading journal for kids",
                              "rationale": "", "meta": {}})()
        r = screen_candidate(cand)
        assert any(f.startswith("trademarked_terms:") for f in r.blocking)

    def test_medical_without_author_is_blocking(self):
        cand = type("C", (), {"niche": "anxiety relief workbook for teenagers with panic symptoms",
                              "rationale": "", "meta": {}})()
        r = screen_candidate(cand)
        assert any(f.startswith("medical_legal_fin:") for f in r.blocking)

    def test_medical_with_author_signal_is_warning_only(self):
        cand = type("C", (), {
            "niche": "anxiety relief workbook for teens",
            "rationale": "authored by a licensed therapist (LCSW clinician)",
            "meta": {},
        })()
        r = screen_candidate(cand)
        assert not r.blocking
        assert any(f.startswith("medical_legal_fin:") for f in r.flags)

    def test_promise_language_is_blocking(self):
        cand = type("C", (), {"niche": "guaranteed weight loss meal planner for busy nurses",
                              "rationale": "", "meta": {}})()
        r = screen_candidate(cand)
        assert "misleading_claims" in r.blocking

    def test_thin_content_with_differentiation_warns_only(self):
        cand = type("C", (), {
            "niche": "guided grief journal for widowed fathers in year one",
            "rationale": "unique structured prompts differentiate it from blank journals",
            "meta": {},
        })()
        r = screen_candidate(cand)
        assert "thin_content" not in r.blocking

    def test_minor_safety_is_absolute(self):
        cand = type("C", (), {"niche": "sexy roleplay journal for teens", "rationale": "",
                              "meta": {}})()
        r = screen_candidate(cand)
        assert "adult_minors" in r.blocking

    def test_screening_persisted_as_assessment(self, db, session_id):
        cand = type("C", (), {"id": "c1", "niche": "harry potter journal", "rationale": "",
                              "meta": {}})()
        r = screen_candidate(cand)
        record_screening(session_id, "c1", r, IntegrityAssessmentStore(db))
        rows = IntegrityAssessmentStore(db).list(session_id, kind="kdp_risk")
        assert rows and rows[0].outcome == "fail"


# ------------------------------------------------------- verification engine
def _evidence(n: int, marketplace: str = "us") -> list[dict]:
    return [
        {
            "id": f"ev{i}",
            "kind": "search_results",
            "marketplace": marketplace,
            "title": "search results",
            "url": f"https://www.amazon.com/s?k=grief&page={i}",
            "data": {"results": [{"asin": f"A{i}", "review_count": 40 + i}]},
            "captured_at": "2026-09-18T00:00:00+00:00",
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


class TestVerificationEngine:
    def _engine(self, db):
        return VerificationEngine(
            EvidenceThresholds(), ClaimStore(db), VerificationStore(db),
            IntegrityAssessmentStore(db),
        )

    def test_well_evidenced_candidate_verifies(self, db, session_id):
        cand = CandidateStore(db).create(
            session_id, niche="sudden-loss grief journals for adult children", marketplace="us")
        _supported_claims(db, session_id, cand.id, ["ev1", "ev2"])
        rep = self._engine(db).verify(session_id, cand, evidence_records=_evidence(3))
        assert rep.verdict == "verified"
        assert rep.status == "passed"
        vers = VerificationStore(db).for_candidate(session_id, cand.id)
        assert vers and vers[0].status == "passed"
        audits = IntegrityAssessmentStore(db).list(session_id, kind="verification")
        assert any(a.outcome == "pass" for a in audits)

    def test_unsupported_claims_fail_verification(self, db, session_id):
        cand = CandidateStore(db).create(session_id, niche="90-day prayer journals for new believers")
        ClaimStore(db).create(session_id, kind="demand", statement="demand looks strong here",
                              candidate_id=cand.id)  # no evidence
        rep = self._engine(db).verify(session_id, cand, evidence_records=_evidence(2))
        assert rep.verdict == "rejected"
        assert any("supported-claim ratio" in p for p in rep.problems)

    def test_model_reject_is_respected(self, db, session_id):
        cand = CandidateStore(db).create(session_id, niche="sudden-loss grief journals for adults")
        _supported_claims(db, session_id, cand.id, ["ev1", "ev2"])
        rep = self._engine(db).verify(session_id, cand, evidence_records=_evidence(3),
                                      model_verdict="reject")
        assert rep.verdict == "rejected"

    def test_kdp_blocking_risk_rejects_even_good_evidence(self, db, session_id):
        cand = CandidateStore(db).create(
            session_id, niche="harry potter guided reading journal for middle schoolers")
        _supported_claims(db, session_id, cand.id, ["ev1", "ev2"])
        rep = self._engine(db).verify(session_id, cand, evidence_records=_evidence(3))
        assert rep.verdict == "rejected"
        assert any("KDP risk" in p for p in rep.problems)

    def test_apply_moves_candidate_status(self, db, session_id):
        cand = CandidateStore(db).create(session_id, niche="sudden-loss grief journals for adults")
        _supported_claims(db, session_id, cand.id, ["ev1", "ev2"])
        eng = self._engine(db)
        rep = eng.verify(session_id, cand, evidence_records=_evidence(3))
        assert eng.apply(session_id, rep, candidates=CandidateStore(db)) == CAND_VERIFIED
        assert CandidateStore(db).get(session_id, cand.id).status == CAND_VERIFIED

        cand2 = CandidateStore(db).create(session_id, niche="productivity journal for lawyers")
        # Model reject conviction → rejected status.
        rep2 = eng.verify(session_id, cand2, evidence_records=_evidence(2), model_verdict="reject")
        eng.apply(session_id, rep2, candidates=CandidateStore(db))
        assert CandidateStore(db).get(session_id, cand2.id).status == CAND_REJECTED

        # Zero data → inconclusive → stays under verification (needs research,
        # not conviction on an empty record).
        cand3 = CandidateStore(db).create(session_id, niche="90-day prayer journals for new believers")
        rep3 = eng.verify(session_id, cand3, evidence_records=[])
        assert rep3.verdict == "inconclusive"
        eng.apply(session_id, rep3, candidates=CandidateStore(db))
        assert CandidateStore(db).get(session_id, cand3.id).status == CAND_UNDER_VERIFICATION

    def test_evidence_for_candidate_relevance(self):
        cand = type("C", (), {"niche": "sudden-loss grief journals",
                              "marketplace": "uk", "id": "c1"})()
        all_ev = [
            # Matches by 3 distinct head terms.
            {"id": "ev1", "kind": "search_results", "marketplace": "us",
             "title": "sudden loss grief journals search results",
             "url": "https://amazon.com/s?k=grief+journals", "data": {}},
            # Matches by marketplace.
            {"id": "ev2", "kind": "product_page", "marketplace": "uk",
             "title": "unrelated", "url": "https://amazon.co.uk/dp/A1", "data": {}},
            # Matches by claim citation.
            {"id": "ev3", "kind": "reviews", "marketplace": "jp",
             "title": "totally different topic", "url": "https://amazon.co.jp/x", "data": {}},
            # Matches nothing.
            {"id": "ev4", "kind": "page_state", "marketplace": "de",
             "title": "something else entirely", "url": "https://amazon.de/y", "data": {}},
        ]
        claims = [type("Claim", (), {"evidence_ids": ["ev3"]})()]
        got = evidence_for_candidate(cand, all_ev, claims=claims)
        ids = {e["id"] for e in got}
        assert ids == {"ev1", "ev2", "ev3"}


# ------------------------------------------------------------------- filters
class TestCandidateFilters:
    def _engine(self, db):
        return CandidateFilterEngine(EvidenceThresholds(), IntegrityAssessmentStore(db))

    def test_zero_data_is_downgrade_not_reject(self, db, session_id):
        """A candidate with NO data at all must be downgraded (needs research),
        not convicted by hard filters on an empty record."""
        cand = CandidateStore(db).create(
            session_id, niche="sudden-loss grief journals for adult children")
        rep = self._engine(db).evaluate(
            session_id, cand, evidence_records=[], claims=[], existing_niches=[]
        )
        assert rep.verdict == "downgrade"
        assert all(f.name not in ("weak_demand", "excessive_competition") for f in rep.failures)

    def test_broad_market_rejected(self, db, session_id):
        cand = CandidateStore(db).create(session_id, niche="productivity")
        ev = _evidence(3)
        claims = _supported_claims(db, session_id, cand.id, ["ev1", "ev2"])
        rep = self._engine(db).evaluate(session_id, cand, evidence_records=ev, claims=claims,
                                        existing_niches=[])
        assert rep.verdict == "reject"
        assert any(f.name == "overly_broad_market" for f in rep.failures)
        audits = IntegrityAssessmentStore(db).list(session_id, kind="filter")
        assert any(a.stage == "overly_broad_market" and a.outcome == "fail" for a in audits)

    def test_specific_evidenced_candidate_passes(self, db, session_id):
        from app.research_data import CAND_UNDER_VERIFICATION

        cand = CandidateStore(db).create(
            session_id,
            niche="sudden-loss grief journals for adult children settling estates")
        # Rich evidence: >= 6 distinct ASINs with review counts across 2+ URLs,
        # plus >= 8 reviews across >= 3 distinct products.
        ev = []
        for i in range(4):
            ev.append({
                "id": f"ev{i}", "kind": "search_results", "marketplace": "us",
                "title": "grief journals", "url": f"https://amazon.com/s?k=grief&p={i}",
                "data": {"results": [
                    {"asin": f"A{i}-{j}", "review_count": 30} for j in range(2)
                ]},
                "captured_at": "2026-09-18T00:00:00+00:00",
            })
        for i in range(3):
            ev.append({
                "id": f"evr{i}", "kind": "reviews", "marketplace": "us",
                "title": "reviews", "url": f"https://amazon.com/product-reviews/A{i}",
                "data": {"asin": f"A{i}", "reviews": [
                    {"rating": 2, "text": "no structure"} for _ in range(3)]},
                "captured_at": "2026-09-18T00:00:00+00:00",
            })
        claims = _supported_claims(db, session_id, cand.id, ["ev0", "ev1"])
        rep = self._engine(db).evaluate(session_id, cand, evidence_records=ev, claims=claims,
                                        existing_niches=[])
        assert rep.verdict == "accept", [f.reason for f in rep.failures]

    def test_duplicate_niche_rejected(self, db, session_id):
        cand = CandidateStore(db).create(session_id, niche="sudden-loss grief journals for adults")
        rep = self._engine(db).evaluate(
            session_id, cand,
            evidence_records=_evidence(2),
            claims=_supported_claims(db, session_id, cand.id, ["ev1", "ev2"]),
            existing_niches=["sudden-loss grief journals for adults settling estates"],
        )
        assert rep.verdict == "reject"
        assert any(f.name == "duplicate_opportunity" for f in rep.failures)


# ------------------------------------------------- keyword metadata integrity
class TestKeywordIntegrity:
    def test_clean_keywords_pass(self):
        from app.kdp_risk import screen_keywords

        r = screen_keywords(
            ["grief journal", "sudden loss journal", "bereavement journal for adults"],
            niche="sudden-loss grief journals for adult children",
        )
        assert not r.blocking
        assert r.level == "safe"

    def test_trademark_keyword_blocked(self):
        from app.kdp_risk import screen_keywords

        r = screen_keywords(["disney trip journal"], niche="disney trip journal")
        assert any(f.startswith("keyword_trademark:") for f in r.blocking)
        assert r.level == "reject"

    def test_deceptive_keyword_blocked(self):
        from app.kdp_risk import screen_keywords

        r = screen_keywords(["free grief journal pdf"], niche="grief journals")
        assert any(f.startswith("keyword_deceptive:") for f in r.blocking)

    def test_unrelated_keyword_flagged(self):
        from app.kdp_risk import screen_keywords

        r = screen_keywords(["cryptocurrency trading"], niche="grief journals for adults")
        assert any(f.startswith("keyword_unrelated:") for f in r.flags)
        # Unrelated is a warning, not a hard block — the report explains it.
        assert not any(f.startswith("keyword_unrelated:") for f in r.blocking)

    def test_keyword_screening_persisted(self, db, session_id):
        from app.integrity import KIND_KEYWORD_INTEGRITY
        from app.kdp_risk import record_keyword_screening, screen_keywords

        r = screen_keywords(["harry potter journal"], niche="reading journals")
        record_keyword_screening(session_id, "c9", r, IntegrityAssessmentStore(db))
        rows = IntegrityAssessmentStore(db).list(
            session_id, kind=KIND_KEYWORD_INTEGRITY)
        assert rows and rows[0].outcome == "fail"

    def test_concern_levels_graduated(self):
        from app.kdp_risk import screen_candidate

        clean = screen_candidate(type("C", (), {
            "niche": "guided estate-settlement journal for widowed fathers",
            "rationale": "structured prompts differentiate it from blank journals",
            "meta": {}})())
        assert clean.level == "safe"

        warned = screen_candidate(type("C", (), {
            "niche": "low-content guided grief journal with structured prompts",
            "rationale": "unique angle",
            "meta": {}})())
        assert warned.level in ("concern", "high_concern")
        assert not warned.blocking


# ----------------------------------------------- opportunity gap gate (8th)
class TestOpportunityGapGate:
    def test_gate_runs_and_passes_seeded_session(self, db, config):
        sid = SessionStore(db, config).create(mode="prompt", prompt="x").id
        seed_quality_gates_fixture(db, config, sid)
        report = _gate_runner(db, sid).run(sid)
        gap = next(g for g in report.results if g.name == "opportunity_gap_gate")
        assert gap.passed, gap.reason

    def test_unevidenced_gap_fails_gate(self, db, config):
        sid = SessionStore(db, config).create(mode="prompt", prompt="x").id
        cand_id = seed_quality_gates_fixture(db, config, sid)
        # Wipe the candidate's claims: the gap no longer traces to evidence.
        with db.tx() as conn:
            conn.execute(
                "DELETE FROM claims WHERE session_id = ? AND candidate_id = ?",
                (sid, cand_id))
        report = _gate_runner(db, sid).run(sid)
        gap = next(g for g in report.results if g.name == "opportunity_gap_gate")
        assert not gap.passed
        assert gap.blocking


def seed_quality_gates_fixture(db, config, sid) -> str:
    """Reuse test_quality_gates._seed without a circular import at module top."""
    from tests.test_quality_gates import _seed

    return _seed(db, config, sid)
