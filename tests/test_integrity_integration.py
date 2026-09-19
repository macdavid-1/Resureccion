"""Integration tests: the integrity pipeline inside the research runner, and
the owner-facing integrity API endpoints."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.claims import ClaimStore
from app.integrity import IntegrityAssessmentStore
from app.research_data import (
    CandidateStore,
    OpportunityStore,
    VerificationStore,
)
from app.sessions import SessionStore
from tests.test_research_runner import (  # noqa: F401
    PHASE_OUTPUTS,
    ScriptedModel,
    make_runner,
    session as session_fixture,
)


@pytest.fixture()
def session(session_fixture):  # noqa: F811
    return session_fixture


@pytest.mark.asyncio
async def test_integrity_pipeline_runs_after_phase8(db, config, session):
    """A full e2e run must execute the integrity pipeline after adversarial
    verification and record the outcome durably."""
    from app.recovery import CheckpointStore

    model = ScriptedModel(PHASE_OUTPUTS)
    runner = make_runner(db, config, model)
    await runner.run(session)
    s = SessionStore(db, config).require(session)
    assert s.status == "completed", s.error

    # Pipeline checkpoint recorded.
    cp = CheckpointStore(db).get(session, "integrity_pipeline")
    assert cp is not None
    assert "accepted" in cp.payload or "rejected" in cp.payload

    # Every non-rejected candidate was verified by the deterministic engine.
    vers = VerificationStore(db).list(session)
    assert len(vers) >= 1  # the scripted session has 2 candidates
    audits = IntegrityAssessmentStore(db).list(session)
    assert audits, "integrity assessments must be persisted"

    # The final report carries the validation verdict in its meta.
    from app.reports import ReportStore
    reps = ReportStore(db, config).list(session)
    final = next(r for r in reps if r.kind == "final")
    assert final.meta.get("mode") in ("final", "provisional")
    assert "validation" in final.meta


@pytest.mark.asyncio
async def test_pipeline_registers_claims_from_phase_analyses(db, config, session):
    model = ScriptedModel(PHASE_OUTPUTS)
    runner = make_runner(db, config, model)
    await runner.run(session)
    s = SessionStore(db, config).require(session)
    claims = ClaimStore(db).list(session, limit=200)
    kinds = {c.kind for c in claims}
    # The scripted phase outputs mention "sudden-loss grief journals" in the
    # competition/consumer/demand/gap analyses → claims must be registered.
    assert s.status == "completed", s.error
    assert "competition" in kinds or "demand" in kinds, (
        f"status={s.status} kinds={kinds} "
        f"cands={[(c.niche[:30], c.status) for c in CandidateStore(db).list(session)]}"
    )


@pytest.mark.asyncio
async def test_verification_status_in_opportunity_meta_is_engine_authoritative(db, config, session):
    model = ScriptedModel(PHASE_OUTPUTS)
    runner = make_runner(db, config, model)
    await runner.run(session)
    opps = OpportunityStore(db).list(session)
    assert len(opps) == 1
    # The scripted model claimed verification_status="pass"; the meta must
    # carry the ENGINE's verdict instead ("verified_with_limitations" or
    # similar) — or the Phase-8 fallback when no engine verdict exists.
    assert opps[0].meta["verification_status"] != "pass"


# ------------------------------------------------------------- integrity API
class TestIntegrityAPI:
    def _headers(self, client) -> dict:
        res = client.post("/api/auth/login", json={"username": "owner", "password": "hunter2"})
        return {"X-Auth-Token": res.json()["token"]}

    def test_integrity_endpoint_requires_auth(self, client):
        res = client.get("/api/sessions/whatever/integrity")
        assert res.status_code in (401, 403)

    def test_integrity_endpoint_unknown_session_404(self, client):
        headers = self._headers(client)
        res = client.get("/api/sessions/does-not-exist/integrity", headers=headers)
        assert res.status_code == 404

    def test_claims_endpoint_lists_registered_claims(self, client):
        headers = self._headers(client)
        # Create a session via API.
        res = client.post(
            "/api/sessions",
            json={"mode": "prompt", "prompt": "research grief journals"},
            headers=headers,
        )
        sid = res.json()["session"]["id"]
        res = client.get(f"/api/sessions/{sid}/claims", headers=headers)
        assert res.status_code == 200
        assert res.json() == {"claims": []}
        res = client.get(f"/api/sessions/{sid}/integrity", headers=headers)
        assert res.status_code == 200
        assert res.json() == {"assessments": []}
