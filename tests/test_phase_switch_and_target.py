"""Phase switching + niche-target rediscovery.

Covers the owner-facing guarantees:
- the AI can switch phases at will ('goto_phase') without losing anything,
- a stated niche target re-opens the earlier phases after verification so
  the run keeps going until the target is met (bounded),
- verified candidates are never re-verified; rejected niches stay rejected,
- a re-run phase MERGES with what it already produced instead of going
  back empty,
- the report's measured keyword/category tables survive a niche-label
  mismatch between the dossier and the dive.
"""
from __future__ import annotations

import time

import pytest

from app.events import EventLog
from app.methodology import PHASE_ORDER
from app.recovery import CheckpointStore
from app.reports import ReportStore
from app.research_data import CandidateStore, OpportunityStore, VerificationStore
from app.sessions import SessionStore

from tests.test_research_runner import (
    PHASE_OUTPUTS,
    RejectAllModel,
    ScriptedModel,
    make_runner,
)


@pytest.fixture()
def session(db, config) -> str:
    sessions = SessionStore(db, config)
    config.max_concurrent_sessions = 3
    s = sessions.create(
        mode="prompt",
        prompt="research grief journals for adults",
        objective="find niches",
    )
    return s.id


class GotoModel(ScriptedModel):
    """Sends exactly one 'goto_phase' out of Phase 2, back to discovery.

    Like the real model, the switch is a COMMIT reply (no 'actions') — the
    orchestrator only honors goto on a commit.
    """

    def __init__(self, script):
        super().__init__(script)
        self.goto_sent = False

    def _reply_for(self, phase: str, user: str):
        reply = super()._reply_for(phase, user)
        if phase == "aggressive_niching" and not self.goto_sent:
            self.goto_sent = True
            reply.content.pop("actions", None)
            reply.content["goto_phase"] = "opportunity_discovery"
        return reply


@pytest.mark.asyncio
async def test_model_can_switch_phase_at_will_without_losing_data(db, config, session):
    sessions = SessionStore(db, config)
    model = GotoModel(PHASE_OUTPUTS)
    runner = make_runner(db, config, model)
    await runner.run(session)

    assert sessions.require(session).status == "completed", (
        sessions.require(session).error
    )

    # The switch is durable: event + jump checkpoint.
    events = EventLog(db).tail(session)
    switches = [e for e in events if e.action == "phase_switched"]
    assert len(switches) == 1
    assert switches[0].detail == {
        "from": "aggressive_niching",
        "to": "opportunity_discovery",
        "pass": 0,
    }
    cp = CheckpointStore(db).get(session, "phase_gotos")
    assert cp is not None and len(cp.payload["jumps"]) == 1

    # Discovery actually re-ran right after the niching call...
    i2 = model.calls.index("aggressive_niching")
    assert model.calls[i2 + 1] == "opportunity_discovery"
    # ...and the whole methodology still completed after the jump.
    assert set(model.calls) == set(PHASE_ORDER)

    # Nothing was lost: the candidate ledger survived the re-run, with no
    # duplicate row for a niche the re-run re-listed.
    cands = CandidateStore(db).list(session, limit=100)
    assert len(cands) >= 3
    niches = [c.niche.strip().lower() for c in cands]
    assert len(niches) == len(set(niches)), niches

    # The report still lands, and the final opportunity was not duplicated.
    assert OpportunityStore(db).count(session) == 1
    assert any(r.kind == "final" for r in ReportStore(db, config).list(session))


@pytest.mark.asyncio
async def test_phase_switch_is_capped_durably(db, config, session):
    model = GotoModel(PHASE_OUTPUTS)
    runner = make_runner(db, config, model)
    runner.rc.max_phase_jumps = 0  # budget spent before the first jump
    await runner.run(session)

    assert SessionStore(db, config).require(session).status == "completed"
    events = EventLog(db).tail(session)
    assert not any(e.action == "phase_switched" for e in events)
    assert any(e.action == "phase_switch_ignored_budget" for e in events)


def test_merge_phase_output_keeps_prior_work():
    from app.research_runner import _merge_phase_output

    prior = {
        "niches": [{"niche": "a"}, {"niche": "b"}],
        "verdict": "strong demand",
        "notes": "first pass",
    }
    new = {
        "niches": [{"niche": "b"}, {"niche": "c"}],
        "verdict": "",
        "notes": "second pass",
    }
    merged = _merge_phase_output(prior, new)
    assert [n["niche"] for n in merged["niches"]] == ["a", "b", "c"]
    # A blank new value never erases prior content; a real one wins.
    assert merged["verdict"] == "strong demand"
    assert merged["notes"] == "second pass"
    # Degenerate inputs stay identity-like: never invent content.
    assert _merge_phase_output(None, new) == new
    assert _merge_phase_output(prior, {}) == prior


def test_niche_target_is_parsed_from_the_brief(db, config, session):
    runner = make_runner(db, config, ScriptedModel(PHASE_OUTPUTS))

    class Brief:
        prompt = "I need 10 niches to get started"
        objective = ""

    class TargetWithKeywords:
        # The report's other numbers must never become a niche target.
        prompt = "report must contain the 20 keywords and ten categories"
        objective = "niche target of 12"

    class NoTarget:
        prompt = "find good niches for me"
        objective = ""

    assert runner._niche_target(session, Brief()) == 10
    # max across statements; the keywords number does not match
    assert runner._niche_target(session, TargetWithKeywords()) == 12
    # no target stated -> env default, then off
    assert runner._niche_target(session, NoTarget()) == 0
    config.target_niches = 7
    assert runner._niche_target(session, NoTarget()) == 7
    assert runner._niche_target(session, Brief()) == 10  # the brief wins


@pytest.mark.asyncio
async def test_target_refill_gate_is_durable_and_bounded(db, config, session):
    runner = make_runner(db, config, ScriptedModel(PHASE_OUTPUTS))
    sessions = SessionStore(db, config)
    s = sessions.require(session)
    events = EventLog(db)

    cands = CandidateStore(db)
    for niche in ("verified niche one", "verified niche two"):
        c = cands.create(session, niche=niche, marketplace="us")
        cands.set_status(session, c.id, "verified")

    # No target stated anywhere -> the gate never fires.
    assert runner._maybe_target_refill(session, s, time.monotonic()) is False

    config.target_niches = 3  # 2 verified < 3 -> short of target

    # No time budget -> skipped and logged, nothing recorded.
    runner.rc.min_remaining_session_fraction_for_pivot = 1.5
    assert runner._maybe_target_refill(session, s, time.monotonic()) is False
    runner.rc.min_remaining_session_fraction_for_pivot = 0.5

    # Target unmet -> refill fires, durably, twice (default budget).
    assert runner._maybe_target_refill(session, s, time.monotonic()) is True
    assert runner._target_refill_rounds(session) == 1
    assert runner._maybe_target_refill(session, s, time.monotonic()) is True
    assert runner._target_refill_rounds(session) == 2

    # Budget spent -> no third round.
    assert runner._maybe_target_refill(session, s, time.monotonic()) is False

    # Target met -> no refill at all.
    config.max_target_refills = 5
    config.target_niches = 2
    assert runner._maybe_target_refill(session, s, time.monotonic()) is False

    actions = [e.action for e in events.tail(session)]
    assert actions.count("target_refill") == 2
    assert "target_refill_skipped_no_time_budget" in actions
    assert "target_refill_budget_exhausted" in actions
    cp = CheckpointStore(db).get(session, "target_refills")
    assert cp is not None and len(cp.payload["refills"]) == 2
    assert cp.payload["refills"][0]["target"] == 3


@pytest.mark.asyncio
async def test_integrity_pipeline_never_reverifies_the_verified(db, config, session):
    runner = make_runner(db, config, ScriptedModel(PHASE_OUTPUTS))
    cands = CandidateStore(db)
    done = cands.create(session, niche="already verified niche", marketplace="us")
    cands.set_status(session, done.id, "verified")
    cands.create(session, niche="fresh candidate niche", marketplace="us")
    verifs = VerificationStore(db)

    runner._run_integrity_pipeline(session, {})
    # The verified candidate was NOT re-judged...
    assert verifs.for_candidate(session, done.id) == []
    assert cands.get(session, done.id).status == "verified"
    done_events = [
        e for e in EventLog(db).tail(session)
        if e.action == "integrity_pipeline_completed"
    ]
    assert done_events[-1].detail["reused_verified"] == 1

    # A second pass (target refill) still never re-verifies it.
    runner._run_integrity_pipeline(session, {})
    assert verifs.for_candidate(session, done.id) == []
    assert cands.get(session, done.id).status == "verified"


@pytest.mark.asyncio
async def test_target_refill_reruns_discovery_passes_and_keeps_data(
    db, config, session
):
    """Verification rejects everything -> with a target set (and pivots off),
    the earlier phases re-open for one rediscovery round, then the session
    completes with its data intact."""
    config.target_niches = 4
    config.max_target_refills = 1
    config.max_focus_pivots = 0  # the target gate alone decides
    model = RejectAllModel(PHASE_OUTPUTS, db, session)
    runner = make_runner(db, config, model)
    await runner.run(session)

    s = SessionStore(db, config).require(session)
    assert s.status == "completed", s.error

    events = EventLog(db).tail(session)
    refills = [e for e in events if e.action == "target_refill"]
    assert len(refills) == 1  # MAX_TARGET_REFILLS honored
    assert refills[0].detail["target"] == 4
    assert refills[0].detail["verified"] == 0
    # The focus stays — a target shortfall is not a new focus.
    assert not any(e.action == "focus_pivot" for e in events)

    # Discovery RE-RAN in the refill pass, and the model was told why.
    assert model.calls.count("opportunity_discovery") >= 2
    assert any(
        "NICHE TARGET" in p
        for p in model.user_prompts.get("aggressive_niching", [])
    )

    # Nothing was lost across the pass: no duplicate candidate rows...
    niches = [
        c.niche.strip().lower()
        for c in CandidateStore(db).list(session, limit=100)
    ]
    assert niches and len(niches) == len(set(niches)), niches
    # ...prior phase analyses still feed the report...
    assert any(r.kind == "final" for r in ReportStore(db, config).list(session))
    # ...and the pass accounting is durable.
    assert runner._pass_round(session) == 1
    assert runner._target_refill_rounds(session) == 1


def test_backend_research_falls_back_to_the_candidate_niche():
    from app.report_model import _backend_research_for

    dive = {
        "space facts books for kids": {
            "keywords": [{"keyword": "space facts for kids"}],
            "books": [],
            "kindle_store": [],
        }
    }
    cand = type("C", (), {"niche": "space facts books for kids"})()

    # The dossier labels the niche differently than the dive measured it —
    # the measured keywords must still reach the report.
    hit = _backend_research_for(dive, "sudden space facts for children", cand)
    assert hit["keywords"][0]["keyword"] == "space facts for kids"
    # Exact match still works.
    assert _backend_research_for(dive, "Space Facts Books!", cand)
    # No match anywhere -> honest empty (rendered as "not captured").
    assert _backend_research_for(dive, "unrelated niche", None) == {}
