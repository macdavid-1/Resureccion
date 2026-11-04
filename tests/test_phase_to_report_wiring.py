"""End-to-end wiring: every research phase's analysis reaches the report.

The report is a rendering of durable state, so a phase whose output never
appears in it was researched and then thrown away. This walks a full scripted
session and asserts each phase's distinctive finding is present in the final
markdown (discovery's signals are the one exception: they are a starting pool,
and the ledger is where their outcome is reported).
"""
from __future__ import annotations

import pytest

from tests.test_research_runner import (
    PHASE_OUTPUTS,
    ScriptedModel,
    make_runner,
    session,  # noqa: F401 — pytest fixture reuse
)

# (phase, marker from that phase's scripted output)
PHASE_MARKERS: tuple[tuple[str, str], ...] = (
    ("aggressive_niching", "autocomplete shows sustained variants"),
    ("competitive_landscape", "generic prompts"),
    ("consumer_intelligence", "gentle tone"),
    ("consumer_intelligence", "no structure"),
    ("consumer_intelligence", "estate section"),
    ("demand_validation", "sustained demand"),
    ("demand_validation", "review velocity"),
    ("opportunity_gap", "no estate guidance"),
    ("opportunity_gap", "estate checklist"),
    ("cross_market_validation", "fewer titles"),
    ("opportunity_synthesis", "3 incumbents"),
)


@pytest.mark.asyncio
async def test_every_research_phase_reaches_the_report(db, config, session):
    from app.reports import ReportStore
    from app.sessions import SessionStore

    runner = make_runner(db, config, ScriptedModel(PHASE_OUTPUTS))
    await runner.run(session)

    s = SessionStore(db, config).require(session)
    assert s.status == "completed", s.error
    store = ReportStore(db, config)
    final = next(r for r in store.list(session) if r.kind == "final")
    body = store.read_markdown(final)

    missing = [
        f"{phase}: {marker!r}"
        for phase, marker in PHASE_MARKERS
        if marker not in body
    ]
    assert not missing, "phase analysis missing from the report:\n  " + "\n  ".join(missing)

    # The structural sections that make the deliverable complete are present.
    for heading in (
        "## Executive summary",
        "## Opportunity portfolio",
        "## Final candidate playbooks",
        "## Consumer intelligence",
        "## Competitor analysis",
        "## Candidate ledger",
        "## Methodology trace",
        "## Research quality statement",
        "## Evidence appendix",
    ):
        assert heading in body, heading

    # …and every phase is accounted for in the methodology trace.
    from app.methodology import PHASE_TITLES

    for title in PHASE_TITLES.values():
        assert title in body, title
