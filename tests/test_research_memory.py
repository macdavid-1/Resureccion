"""The model's own long-running memory: uncapped, verbatim, every step.

The research model keeps a detailed record of what it is doing — findings,
decisions and their reasons, dead ends, open questions, corrections — and reads
it back IN FULL on every step, across phases, resumes and follow-up runs. The
backend stores it and orders it; it never summarizes or truncates it.
"""
from __future__ import annotations

import pytest

from app.methodology import PHASE_ORDER, SPECS
from app.research_memory import MEMORY_ACTIONS, REMEMBER, ResearchMemoryStore


# ------------------------------------------------------------------- storage
def test_an_entry_is_stored_verbatim_however_long(db) -> None:
    store = ResearchMemoryStore(db)
    body = "FINDING: the estate checklist is the only real gap. " + "detail " * 20_000
    entry = store.append(
        "s1", text=body, phase="opportunity_gap", kind="finding",
    )
    assert entry.text == body  # nothing trimmed, nothing capped
    assert len(entry.text) > 140_000
    listed = store.list("s1")
    assert [e.text for e in listed] == [body]
    assert listed[0].phase == "opportunity_gap" and listed[0].kind == "finding"
    assert store.count("s1") == 1
    assert store.chars("s1") == len(body)


def test_entries_keep_their_order_and_are_never_dropped(db) -> None:
    store = ResearchMemoryStore(db)
    for i in range(300):
        store.append("s1", text=f"entry {i}: " + "x" * 2_000, kind="plan")
    listed = store.list("s1")
    assert len(listed) == 300
    assert listed[0].text.startswith("entry 0:") and listed[-1].text.startswith("entry 299:")


def test_an_empty_entry_is_refused(db) -> None:
    store = ResearchMemoryStore(db)
    with pytest.raises(ValueError):
        store.append("s1", text="   ")


def test_memory_is_available_in_every_phase() -> None:
    assert MEMORY_ACTIONS == (REMEMBER,)
    for phase in PHASE_ORDER:
        assert REMEMBER in SPECS[phase].allowed_actions, phase


# --------------------------------------------------------------------- run
class _RememberingModel:
    """Writes one long memory entry in Phase 1, like a real run would."""

    def __init__(self, inner, text: str) -> None:
        self.inner = inner
        self.text = text

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def call(self, session_id, *, system, user, **k):
        reply = await self.inner.call(session_id, system=system, user=user, **k)
        if (
            "opportunity_discovery" in system
            and "LONG-MEMORY-MARKER" not in user
        ):
            reply.content["actions"] = [{
                "action": "remember",
                "args": {"text": self.text, "kind": "decision"},
            }]
        return reply


@pytest.mark.asyncio
async def test_the_run_writes_memory_and_reads_it_back_whole(db, config) -> None:
    from test_research_runner import PHASE_OUTPUTS, ScriptedModel, make_runner

    from app.events import EventLog
    from app.sessions import SessionStore
    from app.trace import ActivityTrace

    text = "LONG-MEMORY-MARKER the broad terms are unwinnable. " + "reason " * 5_000
    model = _RememberingModel(ScriptedModel(PHASE_OUTPUTS), text)
    runner = make_runner(db, config, model)
    sid = SessionStore(db, config).create(mode="prompt", prompt="x").id
    await runner.run(sid)

    store = ResearchMemoryStore(db)
    entries = store.list(sid)
    assert len(entries) == 1 and entries[0].kind == "decision"
    assert entries[0].text == text

    # The very next step re-reads it in full — not a summary of it.
    prompts = model.inner.user_prompts["opportunity_discovery"]
    assert "YOUR LONG-RUNNING MEMORY" in prompts[1]
    assert prompts[1].count("reason ") == 5_000
    assert "LONG-MEMORY-MARKER" in prompts[1]

    actions = [e.action for e in EventLog(db).tail(sid)]
    assert "memory_written" in actions
    traced = [t.text for t in ActivityTrace(db).every(sid)]
    assert any("its own memory" in t for t in traced)


@pytest.mark.asyncio
async def test_a_reply_can_consolidate_memory_in_its_optional_key(db, config) -> None:
    """The model may also hand over a long-form `memory` key on a reply —
    stored verbatim, and never leaked into the committed phase output."""
    from test_research_runner import PHASE_OUTPUTS, ScriptedModel, make_runner

    from app.sessions import SessionStore

    body = "CONSOLIDATED " + "z" * 40_000

    class Consolidating(ScriptedModel):
        def _reply_for(self, phase, user):
            reply = super()._reply_for(phase, user)
            if phase == "opportunity_discovery":
                reply.content["memory"] = [{"text": body, "kind": "plan"}]
            return reply

    model = Consolidating(PHASE_OUTPUTS)
    runner = make_runner(db, config, model)
    sid = SessionStore(db, config).create(mode="prompt", prompt="x").id
    await runner.run(sid)

    entries = ResearchMemoryStore(db).list(sid)
    assert entries, "the optional memory key was not captured"
    assert entries[0].text == body and entries[0].kind == "plan"
    assert entries[0].phase == "opportunity_discovery"
    committed = runner.checkpoints.get_safe(sid, "phase_output:opportunity_discovery")
    assert "memory" not in (committed.payload.get("output") or {})


@pytest.mark.asyncio
async def test_a_follow_up_inherits_the_previous_runs_memory(client) -> None:
    """A continuation's memory is the parent's entries verbatim, in order."""
    res = client.post(
        "/api/auth/login", json={"username": "owner", "password": "hunter2"}
    )
    headers = {"X-Auth-Token": res.json()["token"]}
    parent = client.post(
        "/api/sessions", headers=headers,
        json={"mode": "prompt", "prompt": "research grief journals"},
    ).json()["session"]["id"]
    st = client.app.state
    st.research_memory.append(
        parent, text="decision: drop the prayer niche, demand is seasonal",
        phase="demand_validation", kind="decision",
    )
    st.research_memory.append(
        parent, text="plan: check whether estate guidance is missing from all top 10",
        phase="opportunity_gap", kind="plan",
    )

    preview = client.get(f"/api/sessions/{parent}/continuation", headers=headers)
    assert preview.status_code == 200
    assert preview.json()["memory_entries"] == 2

    child = client.post(
        f"/api/sessions/{parent}/follow-up", headers=headers, json={}
    ).json()["session"]["id"]
    inherited = st.research_memory.list(child)
    assert [e.text for e in inherited] == [
        "decision: drop the prayer niche, demand is seasonal",
        "plan: check whether estate guidance is missing from all top 10",
    ]
    assert all(e.origin == parent for e in inherited)
    assert [e.kind for e in inherited] == ["decision", "plan"]
    cp = st.checkpoints.get_safe(child, "continuation:parent")
    assert len(cp.payload["seeded_memory"]) == 2
