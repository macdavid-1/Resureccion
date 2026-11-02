"""A healthy research run must deliver a FINAL report.

Regression guard for the owner-reported failure: normal research finished
(all 9 phases committed, browser evidence captured) yet the report always
came out PROVISIONAL because the quality gates saw 0 candidates / 0 claims
/ 0 cited evidence — i.e. the phase-analysis → durable-rows pipeline lost
the research, not the gates misfiring.

The scripted session here mirrors a real run: niches discovered, analyses
citing REAL evidence ids, adversarial verdicts, and synthesis. The report
must validate as FINAL (every blocking gate passes).
"""
from __future__ import annotations

import pytest

from tests.test_research_runner import (
    PHASE_OUTPUTS,
    ScriptedModel,
    make_runner,
)
from tests.test_research_runner import (
    session as session_fixture,  # noqa: F401 — pytest fixture re-export
)


@pytest.fixture()
def session(session_fixture):
    return session_fixture


@pytest.mark.asyncio
async def test_healthy_full_run_produces_final_report(db, config, session):
    from app.reports import ReportStore
    from app.sessions import SessionStore

    model = ScriptedModel(PHASE_OUTPUTS)
    runner = make_runner(db, config, model)
    await runner.run(session)

    s = SessionStore(db, config).require(session)
    assert s.status == "completed", s.error

    final = next(r for r in ReportStore(db, config).list(session) if r.kind == "final")
    validation = (final.meta or {}).get("validation") or {}
    assert validation.get("passed") is True, (
        "healthy run produced PROVISIONAL report; failures:\n  - "
        + "\n  - ".join(validation.get("failures") or ["<none recorded>"])
    )
    assert validation.get("mode") == "final"


@pytest.mark.asyncio
async def test_healthy_run_gates_all_pass(db, config, session):
    """The gate runner over a finished healthy session passes every gate."""
    from app.claims import ClaimStore
    from app.integrity import IntegrityAssessmentStore
    from app.quality_gates import QualityGateRunner
    from app.research_data import (
        CandidateStore,
        ObservationStore,
        OpportunityStore,
        VerificationStore,
    )
    from app.sessions import SessionStore
    from app.thresholds import EvidenceThresholds

    model = ScriptedModel(PHASE_OUTPUTS)
    runner = make_runner(db, config, model)
    await runner.run(session)
    s = SessionStore(db, config).require(session)
    assert s.status == "completed", s.error

    be_count = runner.browser_evidence.count(session)
    obs_count = ObservationStore(db).count(session)
    gates = QualityGateRunner(
        EvidenceThresholds(),
        ClaimStore(db),
        CandidateStore(db),
        VerificationStore(db),
        OpportunityStore(db),
        IntegrityAssessmentStore(db),
        browser_evidence_count=be_count,
        observation_count=obs_count,
        evidence_stores=(runner.browser_evidence, runner.evidence),
    ).run(session)
    assert gates.passed, [
        f"{g.name}: {g.reason}" for g in gates.results if not g.passed
    ]


def _drifted_script() -> dict:
    """A realistic model script: niches as plain strings and phases 3-7
    phrasing each niche SHORTER than niching did — the wording drift real
    runs exhibit (and the committed production report shows)."""
    import copy

    from tests.test_research_runner import PHASE_OUTPUTS

    script = copy.deepcopy(PHASE_OUTPUTS)
    full = "children's illustrated facts encyclopedias for ages 8-12"
    script["aggressive_niching"] = [{"niches": [
        full,
        "bizarre and weird facts gift books for adults",
        "teen-oriented life-questions gift books ages 12-17",
    ]}]
    short_labels = {
        "competitive_landscape": "competitive_maps",
        "consumer_intelligence": "consumer_maps",
        "demand_validation": "demand_assessments",
        "opportunity_gap": "gap_analyses",
        "cross_market_validation": "cross_market_checks",
    }
    drifted = ["facts encyclopedias", "weird facts gift books",
               "life-questions gift books"]
    for phase, field in short_labels.items():
        rows = script[phase][0][field]
        for row, label in zip(rows, drifted + rows[len(drifted):]):
            row["niche"] = label
    script["opportunity_synthesis"][0]["opportunities"][0]["niche"] = full
    return script


@pytest.mark.asyncio
async def test_wording_drift_still_produces_final_report(db, config, session):
    """Real models phrase niches differently across phases. That drift must
    not zero out the claims (and thus fail the quality gates) on a run whose
    research genuinely happened."""
    from app.reports import ReportStore
    from app.sessions import SessionStore

    model = ScriptedModel(_drifted_script())
    runner = make_runner(db, config, model)
    await runner.run(session)

    s = SessionStore(db, config).require(session)
    assert s.status == "completed", s.error
    final = next(r for r in ReportStore(db, config).list(session) if r.kind == "final")
    validation = (final.meta or {}).get("validation") or {}
    assert validation.get("passed") is True, (
        "wording drift produced PROVISIONAL report; failures:\n  - "
        + "\n  - ".join(validation.get("failures") or ["<none recorded>"])
    )


@pytest.mark.asyncio
async def test_effects_failure_retries_from_checkpoint(db, config, session):
    """A failure while writing a phase's durable rows must be retried from
    the checkpoint (the phase re-runs and its rows land), not strand the
    phase as 'done' without its data — the exact mechanism that let a
    finished research session deliver a gate-failing report."""
    from app.reports import ReportStore
    from app.research_data import CandidateStore, OpportunityStore
    from app.sessions import SessionStore

    model = ScriptedModel(PHASE_OUTPUTS)
    runner = make_runner(db, config, model)
    original = runner._apply_phase_effects
    state = {"failed": False}

    def flaky(session_id, phase, output):
        if phase == "opportunity_synthesis" and not state["failed"]:
            state["failed"] = True
            raise RuntimeError("injected effects failure")
        return original(session_id, phase, output)

    runner._apply_phase_effects = flaky  # type: ignore[method-assign]
    await runner.run(session)

    assert state["failed"], "injection never fired"
    s = SessionStore(db, config).require(session)
    assert s.status == "completed", s.error
    # The failed phase's rows exist despite the mid-commit failure.
    assert len(OpportunityStore(db).list(session)) == 1
    assert len(CandidateStore(db).list(session)) >= 3
    final = next(r for r in ReportStore(db, config).list(session) if r.kind == "final")
    validation = (final.meta or {}).get("validation") or {}
    assert validation.get("passed") is True, validation.get("failures")
