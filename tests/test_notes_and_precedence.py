"""The model's free-form notes, and owner-over-defaults precedence.

Two owner requirements:

1. While researching, the model must be able to write down what no phase field
   holds — a dead end, a quirk, a tangent, a judgment — and those notes must
   still be there when the final report is written.

2. Every system prompt must say that the owner outranks it: when the owner's
   brief, message or any input contradicts the instructions, the model follows
   the owner — unless the input is evidently a mistake, in which case it keeps
   to the instructions and says so instead of silently obeying.

Notes are annotations, never evidence: they must not satisfy any observation
or evidence floor, and they must never be laundered into report facts.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.research_data import MAX_NOTE_CHARS, ResearchNoteStore
from tests.test_research_runner import session  # noqa: F401 — pytest fixture reuse


def _prompt(**over: Any) -> str:
    from app.methodology import PhaseTiming
    from app.prompts import build_user_prompt

    kwargs: dict[str, Any] = {
        "phase": "opportunity_discovery",
        "mode": "prompt",
        "objective": "",
        "prompt_text": "brief",
        "marketplaces": ["us"],
        "marketplace_mode": "explicit",
        "evidence_digest": [],
        "candidates_digest": [],
        "prior_phase_outputs": None,
        "timing": PhaseTiming(phase="opportunity_discovery", started_at="now"),
    }
    kwargs.update(over)
    return build_user_prompt(**kwargs)  # type: ignore[arg-type]


# ----------------------------------------------------------------- the store
def test_notes_store_dedupes_normalizes_and_bounds(db, session):
    store = ResearchNoteStore(db)
    note, outcome = store.add(
        session, text="  Dead end:   the filler cluster  ", phase="opportunity_discovery"
    )
    assert outcome == "stored"
    assert note is not None
    assert note.text == "Dead end: the filler cluster"
    assert note.phase == "opportunity_discovery"

    # The same note from a later reply is not stored twice (per phase or not).
    _, again = store.add(session, text="Dead end: the filler cluster", phase="aggressive_niching")
    assert again == "duplicate"
    assert store.count(session) == 1

    _, empty = store.add(session, text="   \n  ")
    assert empty == "empty"
    assert store.count(session) == 1

    long_note, _ = store.add(session, text="x" * (MAX_NOTE_CHARS + 500))
    assert long_note is not None
    assert len(long_note.text) == MAX_NOTE_CHARS


def test_notes_store_reports_a_spent_budget_instead_of_dropping_silently(
    db, session, monkeypatch
):
    import app.research_data as rd

    monkeypatch.setattr(rd, "MAX_NOTES_PER_SESSION", 1)
    store = ResearchNoteStore(db)
    assert store.add(session, text="first note")[1] == "stored"
    note, outcome = store.add(session, text="second note")
    assert note is None and outcome == "full"


# ------------------------------------------------------------- runner wiring
@pytest.mark.asyncio
async def test_runner_stores_phase_notes_once_and_never_as_evidence(db, config, session):
    from app.reports import ReportStore
    from tests.test_research_runner import PHASE_OUTPUTS, ScriptedModel, make_runner

    filler = "Every grief journal in that category was a 90-page filler."

    class NoteModel(ScriptedModel):
        """Writes notes on every reply, including a duplicate and a blank."""

        def _reply_for(self, phase: str, user: str):
            reply = super()._reply_for(phase, user)
            reply.content["notes"] = [
                filler,
                {
                    "text": "The Kindle category tree loops back on itself here.",
                    "niche": "grief journals",
                    "kind": "observation",
                },
                {"text": "   "},
                {"text": filler},
            ]
            return reply

    runner = make_runner(db, config, NoteModel(PHASE_OUTPUTS))
    await runner.run(session)

    notes = runner.notes.list(session)
    texts = [n.text for n in notes]
    assert texts.count(filler) == 1, "the same note was stored more than once"
    quirk = next(n for n in notes if n.niche == "grief journals")
    assert quirk.kind == "observation"
    assert all(n.phase for n in notes), "a note lost the phase it came from"
    # Notes are annotations, NOT observations or evidence: no floor may be
    # satisfied by writing prose.
    assert runner.observations.count(session) == 0

    # …and they reach the final report.
    reps = ReportStore(db, config)
    final = [r for r in reps.list(session) if r.kind == "final"]
    assert final, "no final report persisted"
    md = reps.read_markdown(final[0])
    assert "## Research notes written during the run" in md
    assert filler in md
    assert "The Kindle category tree loops back on itself here." in md
    assert "grief journals" in md


# ------------------------------------------------------------------- report
def test_report_renders_the_models_notes_with_attribution():
    from app.report_model import ReportModel, _note_rows, render_markdown

    rows, total = _note_rows([
        {
            "phase": "consumer_intelligence",
            "niche": "grief journals",
            "kind": "note",
            "text": "Reviewers keep asking for a sudden-loss section.",
        },
        {
            "phase": "aggressive_niching",
            "text": "Adjacent niche looked soft but the reviews were thin.",
        },
        {"phase": "opportunity_gap", "text": ""},  # never rendered
    ])
    model = ReportModel(
        session={"id": "s1", "name": "Run", "mode": "prompt"},
        executive_summary={},
        research_notes=rows,
        research_notes_total=total + 4,
    )
    md = render_markdown(model)
    assert "## Research notes written during the run" in md
    assert "Phase 4 — Consumer Intelligence · grief journals" in md
    assert "Reviewers keep asking for a sudden-loss section." in md
    assert "the model's own annotations, not measurements" in md
    assert "4 further note(s) are stored" in md

    from app.pdf_export import model_to_blocks

    _, _, blocks = model_to_blocks(model.to_dict())
    text = " ".join(
        str(b.get("x", "")) for b in blocks if b.get("t") in ("p", "h2", "li")
    )
    assert "Research Notes Written During the Run" in text
    assert "Reviewers keep asking for a sudden-loss section." in text


def test_a_report_stored_before_the_notes_field_still_rerenders():
    from app.report_model import ReportModel, render_markdown

    legacy = ReportModel(
        session={"id": "s1", "name": "Old run", "mode": "prompt"},
        executive_summary={},
    ).to_dict()
    legacy.pop("research_notes", None)
    legacy.pop("research_notes_total", None)
    md = render_markdown(ReportModel.from_dict(legacy))
    assert "Research notes written during the run" not in md


def test_the_writing_stages_receive_the_models_own_notes():
    from app.finalist_playbook import build_playbook_digest
    from app.report_model import ReportModel, _note_rows
    from app.report_narrative import build_digest

    rows, total = _note_rows([
        {
            "phase": "consumer_intelligence",
            "niche": "grief journals",
            "text": "Reviewers keep asking for a sudden-loss section.",
        },
    ])
    model = ReportModel(
        session={"id": "s1", "name": "Run", "mode": "prompt"},
        executive_summary={},
        research_notes=rows,
        research_notes_total=total,
    )
    digest = build_digest(model.to_dict())
    assert digest["research_notes"]
    assert "sudden-loss" in digest["research_notes"][0]["text"]

    playbook = build_playbook_digest({
        "niche": "grief journals for adult daughters",
        "model_notes": [
            {"phase": "consumer_intelligence",
             "text": "Reviewers keep asking for a sudden-loss section."},
        ],
    })
    assert playbook["notes"][0]["text"].startswith("Reviewers keep asking")


def test_candidate_notes_reach_only_their_own_playbook():
    from app.report_model import _dive_index, _finalist_sections

    sections = _finalist_sections(
        cands=[SimpleNamespace(
            id="c1", niche="grief journals", marketplace="us",
            status="verified", meta={},
        )],
        opps=[],
        dive_by_niche=_dive_index([]),
        cons_out=[], gap_out=[], competitors=[], xmarket_out=[],
        run_notes=[
            {"phase": "consumer_intelligence", "niche": "grief journals",
             "text": "grief-specific note"},
            {"phase": "consumer_intelligence", "niche": "space facts books",
             "text": "space-specific note"},
            {"phase": "opportunity_discovery", "niche": "",
             "text": "run-wide note"},
        ],
    )
    texts = [n["text"] for n in sections[0]["model_notes"]]
    assert "grief-specific note" in texts
    assert "run-wide note" in texts
    assert "space-specific note" not in texts


# --------------------------------------------------------------- precedence
def test_every_system_prompt_puts_the_owner_above_the_instructions():
    from app.finalist_playbook import PLAYBOOK_SYSTEM
    from app.methodology import PHASE_ORDER
    from app.prompts import build_system_prompt
    from app.report_narrative import NARRATIVE_SYSTEM

    for phase in PHASE_ORDER:
        system = build_system_prompt(phase)
        # The precedence rule comes FIRST, before any default instruction.
        assert system.startswith("OWNER PRECEDENCE"), phase
        assert "FOLLOW THE OWNER" in system
        # …and keeps the mistake escape hatch.
        assert "MISTAKE" in system
        assert "Silence is not a contradiction" in system
        # The methodology itself is still stated, below the rule.
        assert "METHODOLOGY LAW" in system

    for system in (NARRATIVE_SYSTEM, PLAYBOOK_SYSTEM):
        assert "OWNER PRECEDENCE" in system
        assert "FOLLOW THE OWNER" in system
        assert "MISTAKE" in system


def test_owner_messages_are_told_they_outrank_the_system_prompt():
    prompt = _prompt(owner_messages=["skip cross-market checks, I only sell in the US"])
    assert "OWNER GUIDANCE" in prompt
    assert "skip cross-market checks, I only sell in the US" in prompt
    assert "outrank the system instructions" in prompt
    assert "evidently a mistake" in prompt


def test_the_notes_channel_is_announced_to_the_model():
    from app.prompts import build_system_prompt

    system = build_system_prompt("opportunity_discovery")
    assert "escape hatch" in system
    assert "'notes'" in system
    assert "never evidence" in system

    user = _prompt()
    assert "optional key 'notes'" in user
    # The contract itself is untouched: notes are optional, never required.
    assert "RESPOND WITH JSON ONLY" in user
