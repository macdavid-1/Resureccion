"""End-to-end wiring: every research phase's analysis reaches the report.

A phase whose output never reaches the report was researched and then thrown
away. This walks a full scripted session and asserts each phase's distinctive
finding is present in the dossier the REPORT-WRITING model receives (discovery's
signals are the one exception: they are a starting pool, and the ledger is where
their outcome is reported). It also asserts the delivered body really is the
model's own document rather than the generated fallback layout.
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

    model = ScriptedModel(PHASE_OUTPUTS)
    runner = make_runner(db, config, model)
    await runner.run(session)

    s = SessionStore(db, config).require(session)
    assert s.status == "completed", s.error
    store = ReportStore(db, config)
    final = next(r for r in store.list(session) if r.kind == "final")
    body = store.read_markdown(final)

    assert model.report_prompts, "the model never received the report request"
    dossier = model.report_prompts[0]
    missing = [
        f"{phase}: {marker!r}"
        for phase, marker in PHASE_MARKERS
        if marker not in dossier
    ]
    assert not missing, "phase analysis missing from the report dossier:\n  " + "\n  ".join(missing)

    # The delivered body is the model's own document, not the generated layout.
    assert (final.meta or {}).get("authored_by") == "model"
    assert body.strip()
    assert "# Resurrección — Market Intelligence Report" not in body
    # …and it cites research evidence, so the report stays auditable.
    assert "[ev:" in body or (final.meta or {}).get("evidence_ids")

    # …and every phase is accounted for in the dossier's methodology trace.
    from app.methodology import PHASE_TITLES

    for title in PHASE_TITLES.values():
        assert title in dossier, title
