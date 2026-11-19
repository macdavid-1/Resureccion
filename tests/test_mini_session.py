"""Mini sessions: focused live work approved from a report conversation.

A mini session is not a research run. It is the analyst going to look at one
thing the finished record cannot answer, using the browser, free of the nine
phases — and then coming back to the conversation with the answer. These tests
pin the promises that make it usable:

  - the owner is asked first (a proposal is offered, never started silently);
  - it decides its own steps and is NOT bound by the methodology;
  - it carries the same complete record a chat answer carries;
  - the owner can type into the conversation while it runs and it receives it;
  - its answer lands in the same thread, with citations that resolve;
  - there is no limit on how many may run in one conversation.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from app.mini_session import (
    MINI_SYSTEM,
    MiniSessionService,
    MiniSessionStore,
    parse_reply,
    render_mini_prompt,
)
from app.report_chat import ChatStore, extract_proposal


def _login(client) -> dict:
    res = client.post("/api/auth/login", json={"username": "owner", "password": "hunter2"})
    return {"X-Auth-Token": res.json()["token"]}


async def _run_session(app, *, prompt="grief memoirs for adult children") -> tuple[str, str]:
    import tests.test_research_runner as trr

    session = app.state.sessions.create(mode="prompt", prompt=prompt, objective="find gaps")
    await trr.make_runner(app.state.db, app.state.config, trr.ScriptedModel(trr.PHASE_OUTPUTS)).run(
        session.id
    )
    final = [r for r in app.state.reports.list(session.id) if r.kind == "final"]
    assert final, "scripted run produced no report"
    return session.id, final[0].id


class _FakeExecutor:
    """Stands in for ActionExecutor: records calls, never touches a browser."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.saw_session_ids: list[str] = []

    async def execute(self, *, session_id, action, args):
        self.calls.append((action, dict(args or {})))
        self.saw_session_ids.append(session_id)
        return type(
            "Outcome", (), {
                "ok": True,
                "summary": f"{action} returned 3 results",
                "error": "",
                "evidence_ids": [],
            },
        )()


class _ScriptedMiniModel:
    """Replays JSON replies and records every prompt it was given."""

    def __init__(self, replies: list[dict | str]) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.systems: list[str] = []
        self.labels: list[str] = []

    async def call(self, session_id, *, system, user, **kw):
        self.prompts.append(user)
        self.systems.append(system)
        self.labels.append(str(kw.get("label") or ""))
        reply = type("Reply", (), {})()
        nxt = self.replies.pop(0) if self.replies else {"done": True, "answer": "finished"}
        reply.content = nxt if isinstance(nxt, dict) else None
        reply.raw = nxt if isinstance(nxt, str) else json.dumps(nxt)
        return reply


def _wait_for_mini(client, headers, sid, mid, *, timeout=20.0):
    """Poll the live endpoint until the session stops working."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        res = client.get(f"/api/sessions/{sid}/mini-sessions/{mid}", headers=headers)
        assert res.status_code == 200, res.text
        last = res.json()
        if last["mini_session"]["status"] not in ("queued", "running"):
            return last
        time.sleep(0.02)
    raise AssertionError(f"mini session did not finish: {last}")


@pytest.fixture()
def mini_setup(client):
    headers = _login(client)
    sid, rid = asyncio.run(_run_session(client.app))
    st = client.app.state
    st.mini_executor = _FakeExecutor()
    yield client, headers, sid, rid
    for attr in ("mini_model", "mini_executor", "chat_model"):
        if hasattr(st, attr):
            setattr(st, attr, None)


# ---------------------------------------------------------------- the records
def test_store_roundtrip_and_boot_reconcile(db, config):
    from app.sessions import SessionStore

    sid = SessionStore(db, config).create(mode="prompt", prompt="grief memoirs").id
    store = MiniSessionStore(db)
    mini = store.create(session_id=sid, report_id="r1", goal="check the price", title="price")
    assert mini.status == "queued"
    assert store.active_for_report("r1").id == mini.id

    store.update(mini.id, status="running")
    step = store.add_step(mini.id, kind="action", text="open_product", meta={"ok": True})
    store.add_step(mini.id, kind="owner", text="also check the UK")
    assert [s.id for s in store.steps(mini.id)] == [step.id, step.id + 1]
    assert [s.text for s in store.owner_steps(mini.id)] == ["also check the UK"]
    assert store.steps(mini.id)[0].meta == {"ok": True}

    # A restart must not leave a session claiming to be running forever.
    assert store.reconcile_on_boot() == 1
    stale = store.get(mini.id)
    assert stale.status == "interrupted" and "restarted" in stale.error
    assert store.active_for_report("r1") is None

    # Newest first, and per-report isolation.
    store.create(session_id=sid, report_id="r1", goal="another")
    store.create(session_id=sid, report_id="r2", goal="other report")
    assert len(store.list(sid, "r1")) == 2
    assert len(store.list(sid)) == 3


def test_mini_tables_are_versioned(db):
    with db.read() as conn:
        mini_cols = {r["name"] for r in conn.execute("PRAGMA table_info(mini_sessions)")}
        step_cols = {r["name"] for r in conn.execute("PRAGMA table_info(mini_session_steps)")}
        version = conn.execute("SELECT version FROM schema_version").fetchone()["version"]
    assert {"id", "session_id", "report_id", "goal", "status", "result", "error", "meta"} <= mini_cols
    assert {"mini_id", "kind", "text", "meta"} <= step_cols
    assert version == 10


# ------------------------------------------------------------- the proposal
def test_extract_proposal_splits_the_answer_and_keeps_the_goal():
    text = (
        "The record prices it at $8.99 [ev:abc].\n\n"
        '```mini-session\n{"goal": "Check the current #1 price in the UK",'
        ' "why": "the price may have moved"}\n```'
    )
    prose, proposal = extract_proposal(text)
    assert "```" not in prose and prose.endswith("[ev:abc].")
    assert proposal["goal"] == "Check the current #1 price in the UK"
    assert proposal["why"] == "the price may have moved"


def test_extract_proposal_is_forgiving_but_never_swallows_text():
    # Malformed block: shown, not eaten.
    prose, proposal = extract_proposal("Answer.\n```mini-session\n{not json}\n```")
    assert proposal is None and "mini-session" in prose
    # No goal: no proposal.
    assert extract_proposal('```mini-session\n{"why": "because"}\n```')[1] is None
    # A plain answer is untouched.
    prose, proposal = extract_proposal("Just an answer.")
    assert (prose, proposal) == ("Just an answer.", None)


def test_chat_answer_offers_a_mini_session_without_starting_it(mini_setup):
    client, headers, sid, rid = mini_setup

    class _Proposer(_ScriptedMiniModel):
        async def call(self, session_id, *, system, user, **kw):
            reply = type("Reply", (), {})()
            reply.raw = (
                "I can check the live price.\n\n"
                '```mini-session\n{"goal": "Open the #1 grief memoir and read its price",'
                ' "why": "to see whether $8.99 is still the ceiling"}\n```'
            )
            return reply

    client.app.state.chat_model = _Proposer([])
    res = client.post(
        f"/api/sessions/{sid}/reports/{rid}/chat",
        headers=headers, json={"message": "is that price still right?"},
    )
    assert res.status_code == 200, res.text
    answer = res.json()["answer"]
    assert "mini-session" not in answer["text"], "the block must not reach the bubble"
    assert answer["meta"]["proposal"]["goal"].startswith("Open the #1 grief memoir")

    # Nothing ran: a proposal is an offer.
    listed = client.get(
        f"/api/sessions/{sid}/reports/{rid}/mini-sessions", headers=headers
    ).json()["mini_sessions"]
    assert listed == []


# ------------------------------------------------------------ running the work
def test_mini_session_chooses_its_steps_and_answers_in_the_thread(mini_setup):
    client, headers, sid, rid = mini_setup
    st = client.app.state
    evidence = st.browser_evidence.list(sid, limit=10_000_000)
    cite = evidence[0].id
    chat = ChatStore(st.db)
    asked = chat.add(sid, rid, role="owner", text="how does it compare now?")
    chat.add(sid, rid, role="analyst", text="It sits at $8.99 in a thin field.")

    model = _ScriptedMiniModel([
        {"note": "open the category", "actions": [{"action": "open_category", "args": {"path": "books"}}]},
        {"note": "read the #1 book", "actions": [{"action": "open_product", "args": {"asin": "B00TEST"}}]},
        {
            "note": "settled",
            "actions": [],
            "done": True,
            "answer": f"Still $8.99, unchanged since the run [ev:{cite}].",
        },
    ])
    st.mini_model = model

    res = client.post(
        f"/api/sessions/{sid}/reports/{rid}/mini-sessions",
        headers=headers,
        json={"goal": "Check whether the #1 book's price moved", "proposal_message_id": asked.id},
    )
    assert res.status_code == 200, res.text
    mini_id = res.json()["mini_session"]["id"]
    body = _wait_for_mini(client, headers, sid, mini_id)

    assert body["mini_session"]["status"] == "completed", body["mini_session"]["error"]
    assert "[ev:" in body["mini_session"]["result"]
    assert f"/api/sessions/{sid}/evidence/{cite}" in body["mini_session"]["result"]
    assert body["mini_session"]["meta"]["proposal_message_id"] == asked.id
    assert body["mini_session"]["finished_at"]

    kinds = [s["kind"] for s in body["steps"]]
    assert kinds.count("action") == 2 and kinds[-1] == "answer"
    action_steps = [s for s in body["steps"] if s["kind"] == "action"]
    assert [s["meta"]["summary"] for s in action_steps] == [
        "open_category returned 3 results", "open_product returned 3 results",
    ]
    assert all(s["meta"]["ok"] for s in action_steps)
    assert next(s["text"] for s in body["steps"] if s["kind"] == "note") == "open the category"

    # Both actions ran against the ORIGINAL session, so its evidence ledger and
    # live view keep working.
    assert st.mini_executor.calls == [
        ("open_category", {"path": "books"}), ("open_product", {"asin": "B00TEST"}),
    ]
    assert set(st.mini_executor.saw_session_ids) == {sid}

    # Full context, not a summary: the whole record, the whole thread, and the
    # methodology as reference the model may ignore.
    first_prompt = model.prompts[0]
    for ev in evidence:
        assert ev.id in first_prompt
    assert "It sits at $8.99 in a thin field." in first_prompt
    assert "THE RESEARCH RECORD" in first_prompt
    assert "THE RESEARCH METHODOLOGY (reference only" in first_prompt
    assert "grief memoirs for adult children" in first_prompt
    assert "not bound by" in MINI_SYSTEM.lower() or "NOT INSIDE" in MINI_SYSTEM
    # Steps are carried forward: the second turn sees the first result.
    assert "open_category returned 3 results" in model.prompts[1]

    # The answer is now part of the conversation, marked with its origin.
    rows = ChatStore(st.db).list(sid, rid)
    last = rows[-1]
    assert last.role == "analyst" and "Still $8.99" in last.text
    assert last.meta["mini_session_id"] == mini_id
    assert last.meta["citations"] == [cite]


def test_owner_message_during_the_session_is_received_by_it(mini_setup):
    client, headers, sid, rid = mini_setup
    st = client.app.state
    service: MiniSessionService = st.mini_sessions
    holder: dict[str, str] = {}

    class _OwnerInterruptingModel(_ScriptedMiniModel):
        """Types into the conversation mid-session, then checks it was read."""

        async def call(self, session_id, *, system, user, **kw):
            if not self.prompts:
                service.deliver_owner_message(rid, "focus on the UK marketplace")
                return await super().call(session_id, system=system, user=user, **kw)
            assert "NEW MESSAGES FROM THE OWNER" in user
            assert "focus on the UK marketplace" in user
            return await super().call(session_id, system=system, user=user, **kw)

    st.mini_model = _OwnerInterruptingModel([
        {"note": "start", "actions": [{"action": "search_marketplace", "args": {"query": "grief memoir"}}]},
        {"note": "done", "done": True, "answer": "UK price is £7.99."},
    ])
    res = client.post(
        f"/api/sessions/{sid}/reports/{rid}/mini-sessions",
        headers=headers, json={"goal": "Check the UK price"},
    )
    holder["id"] = res.json()["mini_session"]["id"]
    body = _wait_for_mini(client, headers, sid, holder["id"])
    assert body["mini_session"]["status"] == "completed", body["mini_session"]["error"]
    assert any(s["kind"] == "owner" and "UK marketplace" in s["text"] for s in body["steps"])


def test_a_message_typed_while_it_runs_is_routed_to_it(mini_setup):
    client, headers, sid, rid = mini_setup
    st = client.app.state
    store = MiniSessionStore(st.db)
    mini = store.create(session_id=sid, report_id=rid, goal="check the top price")
    store.update(mini.id, status="running")  # as the service does while working

    res = client.post(
        f"/api/sessions/{sid}/reports/{rid}/chat",
        headers=headers, json={"message": "look at the paperback too"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["delivered_to_mini_session"] == mini.id
    assert body["messages"][-1]["role"] == "owner"
    assert [s.text for s in store.owner_steps(mini.id)] == ["look at the paperback too"]

    # Once it is no longer working, the same conversation answers normally.
    store.update(mini.id, status="completed")

    class _Answerer:
        async def call(self, session_id, *, system, user, **kw):
            return type("Reply", (), {"raw": "Answering as the analyst."})()

    st.chat_model = _Answerer()
    res = client.post(
        f"/api/sessions/{sid}/reports/{rid}/chat",
        headers=headers, json={"message": "and then?"},
    )
    assert res.status_code == 200
    assert "delivered_to_mini_session" not in res.json()
    assert res.json()["answer"]["text"] == "Answering as the analyst."


def test_cancel_stops_the_session(mini_setup):
    client, headers, sid, rid = mini_setup
    st = client.app.state
    service: MiniSessionService = st.mini_sessions
    holder: dict[str, str] = {}

    class _Watcher(_ScriptedMiniModel):
        """Keeps 'working' until the owner cancels, then yields once."""

        async def call(self, session_id, *, system, user, **kw):
            for _ in range(2_000):
                if service._cancel.get(holder.get("id", "")):
                    break
                await asyncio.sleep(0.01)
            return await super().call(session_id, system=system, user=user, **kw)

    st.mini_model = _Watcher([{"note": "working", "actions": [{"action": "reload", "args": {}}]}] * 40)
    res = client.post(
        f"/api/sessions/{sid}/reports/{rid}/mini-sessions",
        headers=headers, json={"goal": "keep going"},
    )
    holder["id"] = res.json()["mini_session"]["id"]
    time.sleep(0.1)
    cancel = client.post(
        f"/api/sessions/{sid}/mini-sessions/{holder['id']}/cancel", headers=headers
    )
    assert cancel.status_code == 200, cancel.text
    body = _wait_for_mini(client, headers, sid, holder["id"])
    assert body["mini_session"]["status"] == "cancelled"
    assert "cancelled by the owner" in body["mini_session"]["error"]
    # Cancelling something that already stopped is refused, not a crash.
    again = client.post(
        f"/api/sessions/{sid}/mini-sessions/{holder['id']}/cancel", headers=headers
    )
    assert again.status_code == 400


def test_a_failing_model_is_recorded_never_silently_dropped(mini_setup):
    client, headers, sid, rid = mini_setup

    class _Broken:
        async def call(self, session_id, *, system, user, **kw):
            raise RuntimeError("model exploded")

    client.app.state.mini_model = _Broken()
    res = client.post(
        f"/api/sessions/{sid}/reports/{rid}/mini-sessions",
        headers=headers, json={"goal": "measure it"},
    )
    mini_id = res.json()["mini_session"]["id"]
    body = _wait_for_mini(client, headers, sid, mini_id)
    assert body["mini_session"]["status"] == "failed"
    assert "model exploded" in body["mini_session"]["error"]
    # A failure is not an answer: nothing was posted into the conversation.
    assert all(m.meta.get("mini_session_id") != mini_id
               for m in ChatStore(client.app.state.db).list(sid, rid))


def test_no_limit_on_mini_sessions_in_one_conversation(mini_setup):
    client, headers, sid, rid = mini_setup
    client.app.state.mini_model = _ScriptedMiniModel([
        {"done": True, "answer": "First answer."},
        {"done": True, "answer": "Second answer."},
        {"done": True, "answer": "Third answer."},
    ])
    ids = []
    for i in range(3):
        res = client.post(
            f"/api/sessions/{sid}/reports/{rid}/mini-sessions",
            headers=headers, json={"goal": f"task {i}"},
        )
        assert res.status_code == 200, res.text
        ids.append(res.json()["mini_session"]["id"])
        _wait_for_mini(client, headers, sid, ids[-1])
    listed = client.get(
        f"/api/sessions/{sid}/reports/{rid}/mini-sessions", headers=headers
    ).json()["mini_sessions"]
    assert {m["id"] for m in listed} == set(ids)
    assert len(listed) == 3
    assert all(m["status"] == "completed" for m in listed)
    answers = [m for m in ChatStore(client.app.state.db).list(sid, rid)
               if m.meta.get("mini_session_id")]
    assert len(answers) == 3


def test_mini_session_endpoints_require_auth_and_check_ownership(mini_setup):
    client, headers, sid, rid = mini_setup
    from fastapi.testclient import TestClient

    fresh = TestClient(client.app)
    assert fresh.post(
        f"/api/sessions/{sid}/reports/{rid}/mini-sessions", json={"goal": "x"}
    ).status_code == 401
    assert fresh.get(
        f"/api/sessions/{sid}/reports/{rid}/mini-sessions"
    ).status_code == 401

    assert client.post(
        f"/api/sessions/{sid}/reports/{rid}/mini-sessions",
        headers=headers, json={"goal": "  "},
    ).status_code == 400
    assert client.get(
        f"/api/sessions/{sid}/mini-sessions/nope", headers=headers
    ).status_code == 404
    assert client.post(
        f"/api/sessions/{sid}/mini-sessions/nope/cancel", headers=headers
    ).status_code == 404


# ---------------------------------------------------------------- the prompt
def test_reply_parsing_is_forgiving():
    note, actions, done, _answer = parse_reply(
        '{"note": "n", "actions": [{"action": "reload"}, {"args": {}}],'
        ' "done": false, "answer": ""}'
    )
    assert (note, actions, done) == ("n", [{"action": "reload", "args": {}}], False)
    # An answer with nothing to run means the job is done.
    assert parse_reply({"answer": "here it is"})[2:] == (True, "here it is")
    # Garbage does not crash the loop.
    assert parse_reply("not json") == ("", [], False, "")


def test_render_mini_prompt_carries_record_thread_steps_and_owner_notes():
    dossier = {"evidence": [{"id": "abc", "data": {"text": "x" * 4_000}}],
               "report": {"markdown": "# T\n\nBody"}}
    prompt = render_mini_prompt(
        dossier=dossier,
        thread=[{"role": "owner", "text": "who buys this?"},
                {"role": "analyst", "text": "adult children"}],
        goal="check the price",
        steps=[],
        owner_notes=["focus on the UK"],
    )
    assert "x" * 4_000 in prompt, "the record is never truncated"
    assert "who buys this?" in prompt and "adult children" in prompt
    assert "check the price" in prompt
    assert "focus on the UK" in prompt
    assert "NOT bound" in prompt
