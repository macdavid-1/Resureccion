"""Final intelligence layer: structured report model, rendering, persistence.

The report is a structured model built deterministically from durable
research state, rendered to markdown, and persisted for re-rendering. These
tests verify the model against a REAL scripted research run (not fixtures
alone), so the rendered deliverable is checked end-to-end.
"""
from __future__ import annotations

import json

import pytest

from tests.test_research_runner import PHASE_OUTPUTS, make_runner
from tests.test_research_runner import session  # noqa: F401 — pytest fixture reuse


async def _run_session(db, config, session_id: str):
    """Drive a full scripted research run to completion."""
    from tests.test_research_runner import ScriptedModel

    runner = make_runner(db, config, ScriptedModel(PHASE_OUTPUTS))
    await runner.run(session_id)


def _final_report(db, config, session_id: str):
    from app.reports import ReportStore

    reps = ReportStore(db, config).list(session_id)
    final = [r for r in reps if r.kind == "final"]
    assert final, "no final report persisted"
    store = ReportStore(db, config)
    return final[0], store.read_markdown(final[0]), store.read_model_json(final[0])


class TestReportModelFromRealRun:
    """The model is built from a complete scripted run — every section."""

    @pytest.mark.asyncio
    async def test_full_run_produces_structured_model(self, db, config, session):
        await _run_session(db, config, session)
        rep, body, model = _final_report(db, config, session)

        assert model is not None, "structured model not persisted"
        assert model["schema_version"] == 2
        assert model["session"]["id"] == session
        # Every required section exists.
        for section in (
            "executive_summary", "opportunities", "consumer_intelligence",
            "competitors", "candidate_ledger", "methodology_trace",
            "evidence_appendix", "quality_statement", "validation",
        ):
            assert section in model, f"missing section: {section}"

    @pytest.mark.asyncio
    async def test_executive_summary_reflects_real_counts(self, db, config, session):
        await _run_session(db, config, session)
        _, body, model = _final_report(db, config, session)

        ex = model["executive_summary"]
        # The scripted run creates 3 candidates, rejects 2, verifies 1.
        assert ex["candidates_considered"] == 3
        assert ex["candidates_rejected"] == 2
        assert ex["survived_verification"] == 1
        assert "9-phase" in ex["methodology"]
        assert ex["major_findings"], "findings list empty"

    @pytest.mark.asyncio
    async def test_opportunity_dossier_is_complete(self, db, config, session):
        await _run_session(db, config, session)
        _, body, model = _final_report(db, config, session)

        opps = model["opportunities"]
        assert len(opps) == 1
        o = opps[0]
        for field in (
            "name", "niche", "parent_market", "target_reader", "reader_problem",
            "marketplaces", "demand", "competitive_landscape", "consumer_needs",
            "market_gap", "differentiation", "risks", "kdp_risk",
            "verification", "confidence", "niching_chain", "marketplace_analysis",
            "angles", "titles", "keywords", "evidence_ids", "next_step",
        ):
            assert field in o, f"dossier missing: {field}"
        # Niching chain shows the hierarchy.
        assert o["niching_chain"], "no niching chain"
        assert len(o["niching_chain"]) >= 2
        # Marketplace analysis is populated from registry + cross-market checks.
        ma = o["marketplace_analysis"]
        assert "us" in ma["observed"]
        assert ma["scope"], "scope missing"
        # Keyword clusters derived.
        assert o["keywords"]["core"], "no core keywords"
        # Next step is concrete.
        assert len(o["next_step"]) > 20

    @pytest.mark.asyncio
    async def test_rendered_markdown_has_all_major_sections(self, db, config, session):
        await _run_session(db, config, session)
        _, body, _ = _final_report(db, config, session)

        for heading in (
            "# Resurrección — Market Intelligence Report",
            "## Executive summary",
            "## Opportunity portfolio",
            "## Consumer intelligence",
            "## Candidate ledger",
            "## Methodology trace",
            "## Research quality statement",
            "## Evidence appendix",
            "**Report status:",
        ):
            assert heading in body, f"rendered report missing: {heading}"
        # Opportunity detail lines.
        assert "**Precise niche:**" in body
        assert "**Niching hierarchy**" in body
        assert "**Marketplace analysis**" in body
        assert "**Evidence-backed book angles**" in body
        assert "**Title concepts**" in body
        assert "**Keyword intelligence**" in body
        assert "**KDP risk" in body
        assert "**Recommended next step:**" in body

    @pytest.mark.asyncio
    async def test_consumer_intelligence_and_quality_statement(self, db, config, session):
        await _run_session(db, config, session)
        _, body, model = _final_report(db, config, session)

        ci = model["consumer_intelligence"]
        themes = ci["themes"]
        # From the scripted P4 output: gentle tone praised, no structure
        # complained about, legal checklist unmet, estate section opportunity.
        assert "gentle tone" in themes["praised"]
        assert "no structure" in themes["complaints"]
        assert "legal checklist" in themes["unmet_needs"]
        assert themes["what_the_new_book_should_do_differently"]
        # Quality statement: certainty policy present, rejections recorded.
        q = model["quality_statement"]
        assert q["rejected"] == 2
        assert q["rejection_reasons"], "rejection reasons missing"
        assert "never manufactured" in q["certainty_policy"]

    @pytest.mark.asyncio
    async def test_competitors_joined_with_browser_evidence(self, db, config, session):
        from app.browser_store import BrowserEvidenceStore

        # Seed a competitor browser-evidence row that P3's map references.
        BrowserEvidenceStore(db).create(
            session_id=session,
            marketplace="us",
            kind="product",
            url="https://www.amazon.com/dp/TEST00001",
            title="A",
            data={"title": "A", "asin": "TEST00001", "price": "$9.99"},
        )
        await _run_session(db, config, session)
        _, body, model = _final_report(db, config, session)

        comp = [c for c in model["competitors"] if c["title"] == "A"]
        assert comp, "P3 competitor not joined with browser evidence"
        c = comp[0]
        assert c["matched_evidence"] is True
        assert c["asin"] == "TEST00001"
        assert c["price"] == "$9.99"
        assert "differentiate" in c["lesson"]


class TestReportPersistenceAndRerender:
    @pytest.mark.asyncio
    async def test_model_json_is_round_trippable(self, db, config, session):
        await _run_session(db, config, session)
        rep, body, model = _final_report(db, config, session)

        from app.report_model import ReportModel, render_markdown

        # from_dict(to_json) must survive the round trip.
        m2 = ReportModel.from_dict(json.loads(json.dumps(model)))
        body2 = render_markdown(m2)
        # Re-rendered body equals the stored body modulo the validation badge
        # (which the runner splices in after rendering).
        assert "## Opportunity portfolio" in body2
        assert "**Precise niche:**" in body2

    @pytest.mark.asyncio
    async def test_rerender_endpoint_produces_new_rendering(self, db, config):
        """API-level: model → markdown re-render round trip."""
        from app.db import Database  # noqa: F401

        # Build model directly, save via store, then re-render via helper.
        from app.reports import ReportStore

        sessions = __import__("app.sessions", fromlist=["SessionStore"]).SessionStore(db, config)
        s = sessions.create(mode="prompt", prompt="rerender probe", objective="o")
        from app.report_model import ReportModel, render_markdown

        model = ReportModel(
            session={"id": s.id, "name": s.name, "mode": "prompt",
                     "marketplaces_requested": ["us"], "objective": "o",
                     "prompt": "rerender probe"},
            executive_summary={"what_was_researched": "x", "why": "y",
                               "marketplaces_investigated": ["us"],
                               "methodology": "m", "candidates_considered": 1,
                               "candidates_rejected": 0, "survived_verification": 1,
                               "demand_verdicts": [], "major_findings": ["f"]},
            opportunities=[],
        )
        body = render_markdown(model)
        store = ReportStore(db, config)
        rep = store.save(session_id := s.id, kind="final",
                         title="Rerender probe", body_markdown=body,
                         meta={"generator": "report_model.v2"})
        store.save_model_json(session_id, report=rep, model_json=model.to_json())
        # Read back and re-render.
        data = store.read_model_json(store.get(s.id, rep.id))
        assert data is not None
        body2 = render_markdown(ReportModel.from_dict(data))
        assert body2 == body, "re-render from model diverged"

    @pytest.mark.asyncio
    async def test_model_endpoint_serves_structured_json(self, client):
        """API-level: run inside the client's app state, fetch the model."""
        from tests.test_research_runner import PHASE_OUTPUTS as PO
        from tests.test_research_runner import ScriptedModel, make_runner

        st = client.app.state
        s = st.sessions.create(mode="prompt", prompt="endpoint probe", objective="o")
        await make_runner(st.db, st.config, ScriptedModel(PO)).run(s.id)
        res = client.post("/api/auth/login", json={"username": "owner", "password": "hunter2"})
        h = {"X-Auth-Token": res.json()["token"]}
        reps = client.get(f"/api/sessions/{s.id}/reports", headers=h).json()["reports"]
        final = [r for r in reps if r["kind"] == "final"]
        assert final, "no final report via API"
        res = client.get(f"/api/sessions/{s.id}/reports/{final[0]['id']}/model", headers=h)
        assert res.status_code == 200
        model = res.json()["model"]
        assert model["schema_version"] == 2
        assert model["session"]["id"] == s.id


class TestPortfolioValidation:
    def test_duplicate_niches_flagged(self, db, config, session):
        from app.claims import ClaimStore
        from app.integrity import IntegrityAssessmentStore
        from app.quality_gates import QualityGateRunner
        from app.report_validation import ReportValidator
        from app.research_data import (
            CandidateStore,
            OpportunityStore,
            VerificationStore,
        )

        candidates = CandidateStore(db)
        opportunities = OpportunityStore(db)
        c1 = candidates.create(session, niche="grief journals for widowed fathers", marketplace="us")
        c2 = candidates.create(session, niche="grief journals for widowed fathers", marketplace="us")
        for c in (c1, c2):
            candidates.set_status(session, c.id, "verified")
            opportunities.create(
                session, candidate_id=c.id, title=f"T {c.id[:4]}", niche=c.niche,
                marketplace="us", confidence=0.7,
                meta={"verification_status": "verified", "evidence_ids": ["e1"]},
            )
        claims = ClaimStore(db)
        validator = ReportValidator(
            claims, opportunities, VerificationStore(db),
            QualityGateRunner(
                __import__("app.thresholds", fromlist=["get_thresholds"]).get_thresholds(),
                claims, candidates, VerificationStore(db), opportunities,
                IntegrityAssessmentStore(db),
                browser_evidence_count=10, observation_count=10,
            ),
            IntegrityAssessmentStore(db),
            candidates=candidates,
        )
        result = validator.validate(session, report_body="")
        dupes = [f for f in result.failures if "duplicated opportunities" in f]
        assert dupes, "duplicate niches not flagged"

    def test_phantom_evidence_ids_flagged(self, db, config, session):
        """Evidence references that do not exist in the session must fail
        validation — the report can never cite fabricated evidence."""
        from app.browser_store import BrowserEvidenceStore
        from app.claims import ClaimStore
        from app.integrity import IntegrityAssessmentStore
        from app.quality_gates import QualityGateRunner
        from app.report_validation import ReportValidator
        from app.research_data import (
            CandidateStore,
            OpportunityStore,
            VerificationStore,
        )

        candidates = CandidateStore(db)
        opportunities = OpportunityStore(db)
        c = candidates.create(session, niche="phantom evidence niche", marketplace="us")
        candidates.set_status(session, c.id, "verified")
        real_id = BrowserEvidenceStore(db).create(
            session_id=session, marketplace="us", kind="product",
            url="https://www.amazon.com/dp/PHANTOM01", title="x", data={},
        ).id
        opportunities.create(
            session, candidate_id=c.id, title="T", niche=c.niche,
            marketplace="us", confidence=0.7,
            meta={"verification_status": "verified",
                  "evidence_ids": [real_id, "fabricated0000000000000000"]},
        )
        claims = ClaimStore(db)
        validator = ReportValidator(
            claims, opportunities, VerificationStore(db),
            QualityGateRunner(
                __import__("app.thresholds", fromlist=["get_thresholds"]).get_thresholds(),
                claims, candidates, VerificationStore(db), opportunities,
                IntegrityAssessmentStore(db),
                browser_evidence_count=10, observation_count=10,
            ),
            IntegrityAssessmentStore(db),
            candidates=candidates,
            browser_evidence=BrowserEvidenceStore(db),
            evidence=__import__("app.research_data", fromlist=["EvidenceStore"]).EvidenceStore(db),
        )
        result = validator.validate(session, report_body="")
        assert any("do not exist in session" in f for f in result.failures)

    def test_unknown_marketplace_flagged(self, db, config, session):
        from app.claims import ClaimStore
        from app.integrity import IntegrityAssessmentStore
        from app.quality_gates import QualityGateRunner
        from app.report_validation import ReportValidator
        from app.research_data import (
            CandidateStore,
            OpportunityStore,
            VerificationStore,
        )

        candidates = CandidateStore(db)
        opportunities = OpportunityStore(db)
        c = candidates.create(session, niche="niche x", marketplace="us")
        candidates.set_status(session, c.id, "verified")
        opportunities.create(
            session, candidate_id=c.id, title="T", niche=c.niche,
            marketplace="us,zz", confidence=0.7,
            meta={"verification_status": "verified"},
        )
        claims = ClaimStore(db)
        validator = ReportValidator(
            claims, opportunities, VerificationStore(db),
            QualityGateRunner(
                __import__("app.thresholds", fromlist=["get_thresholds"]).get_thresholds(),
                claims, candidates, VerificationStore(db), opportunities,
                IntegrityAssessmentStore(db),
                browser_evidence_count=10, observation_count=10,
            ),
            IntegrityAssessmentStore(db),
            candidates=candidates,
        )
        result = validator.validate(session, report_body="")
        assert any("unknown marketplace code 'zz'" in f for f in result.failures)
