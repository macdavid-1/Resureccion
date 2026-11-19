"""Talking to the analyst about a delivered report.

The conversation must (a) carry the WHOLE research record, not a summary,
(b) carry the WHOLE thread so follow-ups keep their meaning, (c) persist per
report, and (d) fail loudly instead of inventing an answer.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.report_chat import ChatStore, build_chat_dossier, render_chat_prompt


def _login(client) -> dict:
    res = client.post("/api/auth/login", json={"username": "owner", "password": "hunter2"})
    return {"X-Auth-Token": res.json()["token"]}


async def _run_session(app, *, prompt="grief memoirs for adult children") -> tuple[str, str]:
    """A completed scripted run; returns (session_id, final_report_id)."""
    import tests.test_research_runner as trr

    db = app.state.db
    cfg = app.state.config
    session = app.state.sessions.create(mode="prompt", prompt=prompt, objective="find gaps")
    await trr.make_runner(db, cfg, trr.ScriptedModel(trr.PHASE_OUTPUTS)).run(session.id)
    final = [r for r in app.state.reports.list(session.id) if r.kind == "final"]
    assert final, "scripted run produced no report"
    return session.id, final[0].id


class _ChatModel:
    """Records every prompt it is given and answers with one known citation."""

    def __init__(self, *, fail: bool = False, delay: float = 0.0) -> None:
        self.prompts: list[str] = []
        self.systems: list[str] = []
        self.fail = fail
        self.delay = delay

    async def call(self, session_id, *, system, user, **kw):
        self.prompts.append(user)
        self.systems.append(system)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("model exploded")
        # Cite a REAL captured record: the evidence array in the dossier, not
        # an arbitrary id earlier in the record (session id, etc.).
        segment = user.split('"evidence": [', 1)[-1]
        ids = list(dict.fromkeys(__import__("re").findall(r'"id":\s*"([0-9a-f]{12,})"', segment)))
        cite = f" [ev:{ids[0]}]" if ids else ""
        reply = type("Reply", (), {})()
        reply.raw = f"The record shows a narrow opening{cite}.\n\n- It is measured."
        return reply


@pytest.fixture()
def chat_setup(client):
    headers = _login(client)
    sid, rid = asyncio.run(_run_session(client.app))
    return client, headers, sid, rid


def test_chat_store_roundtrip_orders_and_validates(db, config):
    from app.sessions import SessionStore

    sid = SessionStore(db, config).create(mode="prompt", prompt="grief memoirs").id
    store = ChatStore(db)
    first = store.add(sid, "r1", role="owner", text="who buys this?")
    second = store.add(sid, "r1", role="analyst", text="adult children", meta={"citations": ["x"]})
    other = store.add(sid, "r2", role="owner", text="a different report")
    rows = store.list(sid, "r1")
    assert [m.id for m in rows] == [first.id, second.id]
    assert rows[1].meta == {"citations": ["x"]}
    assert other.id not in [m.id for m in rows]
    with pytest.raises(ValueError):
        store.add(sid, "r1", role="system", text="nope")


def test_chat_dossier_carries_the_whole_record(client):
    """No sampling: every capture, candidate, phase and the report itself."""
    sid, rid = asyncio.run(_run_session(client.app))
    st = client.app.state
    report = st.reports.get(sid, rid)
    # The analyst's own notes and reasoning are part of the record too.
    st.notes.add(sid, text="Dead end: every grief memoir was a 90-page filler.", phase="aggressive_niching")

    from app.methodology import PHASE_ORDER

    dossier = build_chat_dossier(
        sid, report,
        sessions=st.sessions, candidates=st.candidates,
        opportunities=st.opportunities, verifications=st.verifications,
        claims=st.claims, browser_evidence=st.browser_evidence,
        observations=st.observations, notes=st.notes, thoughts=st.thoughts,
        trace=st.trace, owner_messages=st.owner_messages,
        memory=st.research_memory, reports=st.reports,
        checkpoints=st.checkpoints, phase_order=list(PHASE_ORDER),
    )
    captured = st.browser_evidence.list(sid, limit=10_000_000)
    assert len(dossier["evidence"]) == len(captured)
    assert {e["id"] for e in dossier["evidence"]} == {e.id for e in captured}
    assert len(dossier["candidates"]) == len(st.candidates.list(sid, limit=10_000_000))
    assert dossier["phases"], "per-phase analyses must be carried"
    assert dossier["report"]["markdown"].strip()
    assert dossier["structured_report"], "the structured report model must be carried"
    assert any("90-page filler" in n["text"] for n in dossier["notes"])
    assert dossier["actions"], "the run's operational trace must be carried"


def test_chat_prompt_is_complete_and_carries_the_thread():
    dossier = {
        "evidence": [{"id": "abc123abc123", "data": {"text": "x" * 5000}}],
        "report": {"markdown": "# T\n\nBody", "title": "T"},
    }
    history = [
        type("M", (), {"role": "owner", "text": "who buys this?"})(),
        type("M", (), {"role": "analyst", "text": "adult children in year one"})(),
    ]
    prompt = render_chat_prompt(dossier, history, "and what would they pay?")
    assert "x" * 5000 in prompt, "evidence must not be truncated"
    assert "who buys this?" in prompt and "adult children in year one" in prompt
    assert prompt.rstrip().endswith("and what would they pay?")


class TestChatAPI:
    def test_history_starts_empty_and_requires_auth(self, chat_setup):
        client, headers, sid, rid = chat_setup
        res = client.get(f"/api/sessions/{sid}/reports/{rid}/chat", headers=headers)
        assert res.status_code == 200
        assert res.json()["messages"] == []

    def test_unknown_report_is_404(self, client):
        headers = _login(client)
        assert client.get("/api/sessions/nope/reports/x/chat", headers=headers).status_code == 404

    def test_ask_answer_persist_and_continue_the_thread(self, chat_setup):
        client, headers, sid, rid = chat_setup
        model = _ChatModel()
        client.app.state.chat_model = model

        res = client.post(
            f"/api/sessions/{sid}/reports/{rid}/chat",
            headers=headers, json={"message": "Who exactly buys this?"},
        )
        assert res.status_code == 200, res.text
        body = res.json()

        assert body["question"]["role"] == "owner"
        assert body["answer"]["role"] == "analyst"
        assert "[ev:" in body["answer"]["text"]
        assert body["answer"]["meta"]["citations"], "a real citation resolves"
        assert "/api/sessions/" in body["answer"]["text"], "citations become links"
        assert [m["role"] for m in body["messages"]] == ["owner", "analyst"]

        # The first turn carried EVERY captured record.
        evidence = client.app.state.browser_evidence.list(sid, limit=10_000_000)
        for ev in evidence:
            assert ev.id in model.prompts[0]

        # The second turn carries the thread, so "that one" still means it.
        res2 = client.post(
            f"/api/sessions/{sid}/reports/{rid}/chat",
            headers=headers, json={"message": "And what would they pay?"},
        )
        assert res2.status_code == 200, res2.text
        assert "Who exactly buys this?" in model.prompts[1]
        assert "narrow opening" in model.prompts[1]
        assert len(res2.json()["messages"]) == 4

        # The conversation is durable per report.
        rows = ChatStore(client.app.state.db).list(sid, rid)
        assert [m.role for m in rows] == ["owner", "analyst", "owner", "analyst"]

    def test_empty_message_rejected(self, chat_setup):
        client, headers, sid, rid = chat_setup
        res = client.post(
            f"/api/sessions/{sid}/reports/{rid}/chat", headers=headers, json={"message": "   "}
        )
        assert res.status_code == 400

    def test_model_failure_is_reported_and_the_question_is_kept(self, chat_setup):
        client, headers, sid, rid = chat_setup
        client.app.state.chat_model = _ChatModel(fail=True)
        res = client.post(
            f"/api/sessions/{sid}/reports/{rid}/chat",
            headers=headers, json={"message": "why this niche?"},
        )
        assert res.status_code == 502
        assert "could not answer" in res.json()["detail"]
        rows = ChatStore(client.app.state.db).list(sid, rid)
        assert [m.role for m in rows] == ["owner"], "the question must survive a failed answer"

    def test_timeout_is_reported(self, chat_setup):
        client, headers, sid, rid = chat_setup
        client.app.state.chat_model = _ChatModel(delay=0.4)
        # The route reads the live config value, so a tiny one keeps the suite
        # fast while exercising the real wait_for path.
        client.app.state.config.chat_timeout_seconds = 0.05
        res = client.post(
            f"/api/sessions/{sid}/reports/{rid}/chat",
            headers=headers, json={"message": "still there?"},
        )
        assert res.status_code == 504
        assert "longer than" in res.json()["detail"]
        assert [m.role for m in ChatStore(client.app.state.db).list(sid, rid)] == ["owner"]


def test_unusable_citations_are_dropped_not_linked(chat_setup):
    client, headers, sid, rid = chat_setup

    class _BadCite(_ChatModel):
        async def call(self, session_id, *, system, user, **kw):
            reply = type("Reply", (), {})()
            reply.raw = "Claim one [ev:deadbeefdeadbeef] and two [ev:ffffffffffffffff]."
            return reply

    client.app.state.chat_model = _BadCite()
    res = client.post(
        f"/api/sessions/{sid}/reports/{rid}/chat",
        headers=headers, json={"message": "cite that"},
    )
    assert res.status_code == 200, res.text
    answer = res.json()["answer"]
    assert "deadbeefdeadbeef" not in answer["text"]
    assert answer["meta"]["dropped_citations"] == ["deadbeefdeadbeef", "ffffffffffffffff"]
    assert "Claim one" in answer["text"] and "two" in answer["text"]


def test_chat_messages_table_is_versioned(db):
    with db.read() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(report_chat_messages)").fetchall()}
    assert {"session_id", "report_id", "role", "text", "created_at", "meta"} <= cols
    with db.read() as conn:
        version = conn.execute("SELECT version FROM schema_version").fetchone()["version"]
    assert version == 10


def test_report_markdown_is_json_serialisable_with_chat_meta(chat_setup):
    """The dossier must survive the JSON dump the prompt is built from."""
    client, _headers, sid, rid = chat_setup
    st = client.app.state
    from app.methodology import PHASE_ORDER

    dossier = build_chat_dossier(
        sid, st.reports.get(sid, rid),
        sessions=st.sessions, candidates=st.candidates,
        opportunities=st.opportunities, verifications=st.verifications,
        claims=st.claims, browser_evidence=st.browser_evidence,
        observations=st.observations, notes=st.notes, thoughts=st.thoughts,
        trace=st.trace, owner_messages=st.owner_messages,
        memory=st.research_memory, reports=st.reports,
        checkpoints=st.checkpoints, phase_order=list(PHASE_ORDER),
    )
    blob = json.dumps(dossier, default=str)
    assert len(blob) > 1000
