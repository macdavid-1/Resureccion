"""Accounting + research-depth behaviour.

Two failures this file exists to prevent:

1. BLANK ANALYSIS counted as research — a phase could commit a list whose
   rows were empty strings, so the report showed "eight demand verdicts",
   a consumer table of dashes, and "0 candidates considered" next to four
   researched niches.
2. AN EARLY FINISH — a fixed call ceiling ended runs after an hour or two
   on an 8-12 hour budget, so spare time never became deeper research.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app.methodology import P5_DEMAND, P9_SYNTHESIS, PHASE_ORDER, SPECS
from app.research_runner import ResearchRunner, _primary_items
from app.report_model import _exec_summary, _trace
from app.sessions import SessionStore


# ---------------------------------------------------------------- blank rows
def test_blank_analysis_rows_do_not_count_as_findings() -> None:
    blanks = {"demand_assessments": [{"niche": "", "verdict": "", "signals": []}]}
    assert _primary_items(blanks, "demand_assessments") == 0

    ids_only = {"demand_assessments": [{"niche": "", "evidence_ids": ["abc123"]}]}
    assert _primary_items(ids_only, "demand_assessments") == 0

    real = {"demand_assessments": [{"niche": "ocean facts", "verdict": "sustained"}]}
    assert _primary_items(real, "demand_assessments") == 1

    assert _primary_items({}, "demand_assessments") == 0
    assert _primary_items({"demand_assessments": "not a list"}, "demand_assessments") == 0


def test_content_phases_may_never_commit_an_empty_primary_list() -> None:
    """A phase that DECLARES content owes at least one item, even when its
    upstream input is missing; phases 8 and 9 stay free (the deterministic
    pipeline and the opportunity contract decide those)."""
    for phase in PHASE_ORDER:
        spec = SPECS[phase]
        floor = ResearchRunner._item_floor(spec, 0)
        if spec.min_output_items > 0:
            assert floor >= 1, phase
        else:
            assert floor == 0, phase
    assert ResearchRunner._item_floor(SPECS[P5_DEMAND], 4) == 4
    assert ResearchRunner._item_floor(SPECS[P9_SYNTHESIS], 0) == 0


# ------------------------------------------------------------------ backfill
@pytest.mark.asyncio
async def test_researched_niches_become_candidate_rows(db, config) -> None:
    """Phases 3-7 research niches the niche phase never registered: those
    must land in the ledger, or the report reads "0 candidates considered"
    while discussing researched niches — and the integrity pipeline has no
    row to judge, so claims, rejections and verifications all read zero."""
    from test_research_runner import PHASE_OUTPUTS, ScriptedModel, make_runner

    runner = make_runner(db, config, ScriptedModel(PHASE_OUTPUTS))
    sid = SessionStore(db, config).create(mode="prompt", prompt="x").id

    runner._backfill_candidates(sid, "demand_validation", {
        "demand_assessments": [
            {"niche": "ocean facts encyclopedias ages 8-12",
             "verdict": "sustained demand"},
            {"niche": "", "verdict": ""},          # blank row is ignored
            {"niche": "tiny"},                      # too short to be a niche
        ],
    })
    cands = runner.candidates.list(sid, limit=50)
    assert len(cands) == 1
    assert cands[0].niche == "ocean facts encyclopedias ages 8-12"
    assert (cands[0].meta or {}).get("analysis_field") == "demand_assessments"
    assert (cands[0].meta or {}).get("via") == "demand_validation"

    # A shorter label for the SAME niche must not double-count the ledger.
    runner._backfill_candidates(sid, "consumer_intelligence", {
        "consumer_maps": [{"niche": "ocean facts encyclopedias",
                           "praises": ["colourful"]}],
    })
    assert len(runner.candidates.list(sid, limit=50)) == 1


@pytest.mark.asyncio
async def test_backfill_seeds_only_evidence_that_exists(db, config) -> None:
    """Chain-of-custody: a backfilled candidate may only be seeded with ids
    captured in THIS session — invented ids are dropped, not stored."""
    from test_research_runner import PHASE_OUTPUTS, ScriptedModel, make_runner
    from app.browser_store import BrowserEvidenceStore

    runner = make_runner(db, config, ScriptedModel(PHASE_OUTPUTS))
    sid = SessionStore(db, config).create(mode="prompt", prompt="x").id
    real = BrowserEvidenceStore(db)
    real.create(session_id=sid, marketplace="us", kind="search_results",
                url="https://www.amazon.com/s?k=ocean+facts", title="Ocean facts",
                data={})

    runner._backfill_candidates(sid, "competitive_landscape", {
        "competitive_maps": [{
            "niche": "ocean facts encyclopedias for kids",
            "saturation": "fragmented",
            "evidence_ids": [real.list(sid, limit=1)[0].id, "made_up_id"],
        }],
    })
    cands = runner.candidates.list(sid, limit=50)
    assert len(cands) == 1
    seeded = (cands[0].meta or {}).get("seed_evidence_ids") or []
    assert seeded and "made_up_id" not in seeded


# --------------------------------------------------------------- time budget
def test_depth_floor_raises_while_the_session_is_young(db, config) -> None:
    from test_research_runner import PHASE_OUTPUTS, ScriptedModel, make_runner

    runner = make_runner(db, config, ScriptedModel(PHASE_OUTPUTS))
    now = time.monotonic()
    mult = config.depth_evidence_multiplier
    assert mult > 1
    # Under the minimum: the phase owes the RAISED floor …
    assert runner._evidence_floor(10, now) == 10 * mult
    # … and once the minimum has elapsed the declared floor stands.
    assert runner._evidence_floor(10, now - (config.research_min_hours + 1) * 3600) == 10
    # Phases without an evidence floor are never raised.
    assert runner._evidence_floor(0, now) == 0
    # Opting out returns the declared floor immediately.
    config.research_min_hours = 0
    assert runner._evidence_floor(10, now) == 10


def test_model_call_ceiling_follows_the_time_budget(db, config) -> None:
    """No run may end on a fixed call counter while hours remain."""
    from test_research_runner import PHASE_OUTPUTS, ScriptedModel, make_runner

    runner = make_runner(db, config, ScriptedModel(PHASE_OUTPUTS))
    for mode in ("prompt", "keywords", "auto"):
        assert runner._model_call_ceiling(mode) >= (
            config.session_hard_timeout_hours * config.model_calls_per_hour
        )


# ------------------------------------------------------------ report honesty
def test_exec_summary_states_evidence_and_claims_and_drops_blank_verdicts() -> None:
    session = SimpleNamespace(
        prompt="brief", objective="", marketplaces=["us"], mode="prompt",
    )
    ex = _exec_summary(
        session,
        {"total": 3, "rejected": 1, "verified": 1},
        [{"niche": "ocean facts", "verdict": ""}, {"niche": "", "verdict": ""}],
        {},
        [],
        evidence_records=42,
        claims_registered=7,
    )
    assert ex["demand_verdicts"] == [], "blank verdicts must not be reported"
    assert ex["evidence_records"] == 42
    assert ex["claims_registered"] == 7
    # With no portfolio, recorded demand verdicts still carry the findings.
    ex2 = _exec_summary(
        session, {"total": 1}, [{"niche": "ocean facts", "verdict": "sustained demand"}],
        {}, [],
    )
    assert ex2["major_findings"] and "sustained demand" in ex2["major_findings"][0]


def test_methodology_trace_admits_phases_that_produced_nothing() -> None:
    rows = _trace({
        "opportunity_discovery": {"signals": [{"topic": "x"}]},
        "demand_validation": {},
        "consumer_intelligence": {"consumer_maps": []},
    })
    by_phase = {r["phase"]: r for r in rows}
    assert by_phase["opportunity_discovery"]["completed"] is True
    assert by_phase["demand_validation"]["completed"] is False
    # A contract key present but empty is still "no output".
    assert by_phase["consumer_intelligence"]["completed"] is False
