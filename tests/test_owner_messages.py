"""Owner→agent mid-research messaging: the exactly-once, zero-context-loss
channel. Covers the store semantics, the prompt injection, the live-runner
pickup (non-interrupting, durable across resume), and the API surface.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.events import EventLog
from app.owner_messages import OwnerMessageStore
from app.prompts import build_user_prompt
from app.recovery import CheckpointStore
from app.sessions import SessionStore
from app.trace import ActivityTrace


@pytest.fixture()
def store(db) -> OwnerMessageStore:
    return OwnerMessageStore(db)


@pytest.fixture()
def sid(db, config) -> str:
    return SessionStore(db, config).create(mode="prompt", prompt="brief").id


def _prompt(**over: Any) -> str:
    from app.methodology import PhaseTiming

    kwargs: dict[str, Any] = dict(
        phase="opportunity_discovery",
        mode="prompt",
        objective="",
        prompt_text="brief",
        marketplaces=["us"],
        marketplace_mode="explicit",
        evidence_digest=[],
        candidates_digest=[],
        prior_phase_outputs=None,
        timing=PhaseTiming(phase="opportunity_discovery", started_at="now"),
    )
    kwargs.update(over)
    return build_user_prompt(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------- store
def test_post_and_journal_roundtrip(store: OwnerMessageStore, sid: str) -> None:
    m = store.post(sid, "  focus on   grief journals  ")
    assert m.id > 0
    assert m.text == "focus on grief journals"  # whitespace-normalized
    assert m.delivered_at is None  # created undelivered
    j = store.journal(sid)
    assert len(j) == 1 and j[0].text == "focus on grief journals"
    assert j[0].to_dict()["delivered"] is False


def test_post_rejects_empty(store: OwnerMessageStore, sid: str) -> None:
    with pytest.raises(ValueError):
        store.post(sid, "   ")


def test_consume_pending_is_exactly_once(store: OwnerMessageStore, sid: str) -> None:
    store.post(sid, "first")
    store.post(sid, "second")
    got = store.consume_pending(sid)
    assert [m.text for m in got] == ["first", "second"]  # oldest first
    assert all(m.delivered_at for m in got)
    # Second consume returns nothing: delivery is once, never duplicated.
    assert store.consume_pending(sid) == []
    assert store.count_pending(sid) == 0
    # But the journal keeps everything forever — zero context loss.
    assert len(store.journal(sid)) == 2
    assert all(m.to_dict()["delivered"] for m in store.journal(sid))


def test_consume_scoped_to_session(store: OwnerMessageStore, db, config) -> None:
    s1 = SessionStore(db, config).create(mode="auto").id
    s2 = SessionStore(db, config).create(mode="auto").id
    store.post(s1, "for one")
    store.post(s2, "for two")
    assert [m.text for m in store.consume_pending(s1)] == ["for one"]
    assert [m.text for m in store.consume_pending(s2)] == ["for two"]


def test_mark_delivered_idempotent(store: OwnerMessageStore, sid: str) -> None:
    m = store.post(sid, "x")
    assert store.mark_delivered([m.id]) == 1
    assert store.mark_delivered([m.id]) == 0  # already delivered
    assert store.consume_pending(sid) == []


# -------------------------------------------------------------------- prompt
def test_prompt_includes_owner_messages_and_new_marker() -> None:
    p = _prompt(owner_messages=["(owner, t1) focus on grief journals", "(owner, t2) check UK too"])
    assert "OWNER GUIDANCE" in p
    assert "focus on grief journals" in p
    assert "check UK too" in p
    assert "[new] (owner, t2)" in p  # newest marked as just-delivered
    assert "[new] (owner, t1)" not in p


def test_prompt_without_messages_has_no_block() -> None:
    assert "OWNER GUIDANCE" not in _prompt()
    assert "OWNER GUIDANCE" not in _prompt(owner_messages=[])


# -------------------------------------------------------------------- runner
def _runner(db, config, model):
    from app.actions import ActionExecutor
    from app.agent import AgentStateStore
    from app.artifacts import ArtifactStore
    from app.browser_store import BrowserEvidenceStore
    from app.evidence_capture import EvidenceCollector
    from app.marketplace import AMAZON_MARKETPLACES
    from app.recovery import ErrorStateStore
    from app.research_data import (
        CandidateStore,
        EvidenceStore,
        ObservationStore,
        OpportunityStore,
    )
    from app.research_runner import ResearchRunner
    from app.autonomy import AutonomySupervisor
    from app.trace import ActivityTrace

    events = EventLog(db)
    sessions = SessionStore(db, config)
    agent = AgentStateStore(db, events)
    errors = ErrorStateStore(db)
    checkpoints = CheckpointStore(db)
    be_store = BrowserEvidenceStore(db)

    class FakePage:
        url = "https://www.amazon.com/s?k=test"

    class FakeBrowser:
        def __init__(self) -> None:
            self.page = FakePage()

        async def open_marketplace(self, m: Any, path: str = "/") -> Any:
            return self.page

        async def new_page(self) -> Any:
            return self.page

        async def navigate(self, page: Any, url: str) -> None:
            self.page.url = url

        async def close_page(self, page: Any) -> None:
            pass

        async def screenshot(self, page: Any) -> bytes:
            return b"png"

    class FakeArtifacts:
        def save_bytes(self, *a: Any, **k: Any) -> Any:
            return type("A", (), {"id": "art"})()

    class StubAuth:
        async def _classify_page(self, page: Any, marketplace: Any) -> Any:
            from app.amazon_auth import AuthCheck

            return AuthCheck("unknown", page.url, "t", {})

    class Collector:
        last_screenshot: bytes | None = None

    browser = FakeBrowser()
    executor = ActionExecutor(
        config, browser, EvidenceCollector(browser, be_store, FakeArtifacts()),
        StubAuth(), events,  # type: ignore[arg-type]
    )
    executor.set_marketplaces([AMAZON_MARKETPLACES["us"]])
    supervisor = AutonomySupervisor(
        db, config, sessions=sessions, events=events, errors=errors, checkpoints=checkpoints,
    )
    return ResearchRunner(
        db, config,
        sessions=sessions, agent=agent, events=events, errors=errors,
        checkpoints=checkpoints, candidates=CandidateStore(db), evidence=EvidenceStore(db),
        observations=ObservationStore(db), opportunities=OpportunityStore(db),
        artifacts=ArtifactStore(db, config), browser=browser,  # type: ignore[arg-type]
        browser_evidence=be_store, browser_auth=type("BAS", (), {})(),
        model=model,  # type: ignore[arg-type]
        executor=executor, trace=ActivityTrace(db), supervisor=supervisor,
    )


class _Reply:
    def __init__(self, content: dict[str, Any]) -> None:
        self.content = content
        self.model = "fake"
        self.elapsed_seconds = 0.1
        self.raw = "{}"


class _MinModel:
    """One iteration per phase: empty actions, minimal contract output."""

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.systems: list[str] = []

    async def call(self, session_id: str, *, system: str, user: str, **k: Any) -> Any:
        self.prompts.append(user)
        self.systems.append(system)
        # Every phase's contract includes "analysis" or similar; an empty
        # dict misses required fields, so the runner keeps iterating. To stay
        # deterministic, respond with the minimal keys the FIRST phase asks
        # for; later phases the gates will force-pivot past (stall) — that is
        # fine for message-injection assertions.
        return _Reply({"actions": []})

    async def aclose(self) -> None:
        pass


@pytest.mark.asyncio
async def test_runner_injects_owner_message_without_interruption(db, config, sid: str) -> None:
    """A message posted while 'research runs' lands in the model's next
    prompt, never pauses the run, and stays in every later prompt."""
    from tests.test_research_runner import PHASE_OUTPUTS, ScriptedModel

    class PromptRecordingModel(ScriptedModel):
        """Records every user prompt the model actually received."""

        def __init__(self, script: dict[str, list[dict[str, Any]]]) -> None:
            super().__init__(script)
            self.prompts_seen: list[str] = []

        async def call(self, session_id: str, *, system: str, user: str, **k: Any) -> Any:
            self.prompts_seen.append(user)
            return await super().call(session_id, system=system, user=user, **k)

    model = PromptRecordingModel(PHASE_OUTPUTS)
    runner = _runner(db, config, model)
    # Intercept the first model call: post the message mid-run, exactly like
    # an owner typing while watching the live research process.
    original_call = model.call
    posted = {"done": False}

    async def call_and_post(*a: Any, **k: Any) -> Any:
        reply = await original_call(*a, **k)
        if not posted["done"]:
            posted["done"] = True
            OwnerMessageStore(db).post(sid, "focus on grief journals")
        return reply

    model.call = call_and_post  # type: ignore[method-assign]
    await runner.run(sid)
    sessions = SessionStore(db, config)
    assert sessions.require(sid).status == "completed", sessions.require(sid).error
    # The very first prompt (before the message existed) must NOT contain it;
    # every prompt after consumption must — zero context loss from then on.
    assert model.prompts_seen, "model was called"
    assert "focus on grief journals" not in model.prompts_seen[0]
    after = [p for p in model.prompts_seen[1:]]
    assert after, "runner kept iterating after the message arrived"
    assert all("focus on grief journals" in p and "OWNER GUIDANCE" in p for p in after)
    # Delivered exactly once…
    store = OwnerMessageStore(db)
    assert store.count_pending(sid) == 0
    # …and journaled forever.
    assert len(store.journal(sid)) == 1


@pytest.mark.asyncio
async def test_runner_rehydrates_owner_notes_after_resume(db, config, sid: str) -> None:
    """A resumed session (fresh runner object) sees ALL prior owner notes —
    zero context loss across a crash/restart boundary."""
    from tests.test_research_runner import PHASE_OUTPUTS, ScriptedModel

    store = OwnerMessageStore(db)
    store.post(sid, "prefer evergreen topics")
    store.mark_delivered([store.journal(sid)[0].id])  # already consumed pre-crash
    model = ScriptedModel(PHASE_OUTPUTS)
    runner = _runner(db, config, model)
    await runner.run(sid)
    sessions = SessionStore(db, config)
    assert sessions.require(sid).status == "completed", sessions.require(sid).error
    lines = runner._owner_note_lines_for(sid)
    assert any("prefer evergreen topics" in ln for ln in lines)


@pytest.mark.asyncio
async def test_owner_message_pickup_is_traceable(db, config, sid: str) -> None:
    """The live trace records the pickup so the owner sees delivery in the UI."""
    from tests.test_research_runner import PHASE_OUTPUTS, ScriptedModel

    runner = _runner(db, config, ScriptedModel(PHASE_OUTPUTS))
    events = EventLog(db)
    runner.events = events
    runner.owner_messages.post(sid, "look at the UK market")
    runner._take_owner_messages(sid)
    evs = [e.action for e in events.tail(sid)]
    assert "owner_message_received" in evs
    trace = ActivityTrace(db)
    texts = " ".join(e.text for e in trace.recent(sid, limit=50))
    assert "Owner message received" in texts


# ----------------------------------------------------------------------- API
def test_message_endpoints_auth_and_flow(client) -> None:
    # 401 without credentials (checked BEFORE login: the TestClient keeps the
    # auth cookie afterwards, which would authenticate the request anyway).
    assert client.post("/api/sessions/nope/messages", json={"text": "x"}).status_code == 401
    assert client.get("/api/sessions/nope/messages").status_code == 401

    headers = {
        "X-Auth-Token": client.post(
            "/api/auth/login", json={"username": "owner", "password": "hunter2"}
        ).json()["token"]
    }
    sid = client.post("/api/sessions", headers=headers, json={"mode": "auto"}).json()["session"]["id"]

    # 404 for unknown session.
    res = client.post("/api/sessions/nope/messages", headers=headers, json={"text": "x"})
    assert res.status_code == 404
    # 400 for empty text.
    res = client.post(f"/api/sessions/{sid}/messages", headers=headers, json={"text": "  "})
    assert res.status_code == 400
    # Happy path.
    res = client.post(f"/api/sessions/{sid}/messages", headers=headers, json={"text": "check UK too"})
    assert res.status_code == 200
    assert res.json()["message"]["text"] == "check UK too"
    assert res.json()["message"]["delivered"] is False
    # Journal lists it.
    res = client.get(f"/api/sessions/{sid}/messages", headers=headers)
    assert res.status_code == 200
    msgs = res.json()["messages"]
    assert len(msgs) == 1 and msgs[0]["text"] == "check UK too"
