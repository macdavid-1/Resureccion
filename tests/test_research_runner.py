"""End-to-end runner tests with fake model + fake browser.

Verifies the deterministic methodology is enforced:
- phases run in order with gate checks,
- the runner produces candidates and validated opportunities from model output,
- auth pause, model failure, and cancel paths land in durable statuses,
- the registry spawns/cancels and enforces the concurrency cap.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.actions import ActionExecutor, AuthPauseRequired
from app.agent import AgentStateStore
from app.artifacts import ArtifactStore
from app.browser_store import BrowserEvidenceStore
from app.config import Config
from app.db import Database
from app.evidence_capture import EvidenceCollector
from app.events import EventLog
from app.jobs import JobOrchestrator
from app.marketplace import AMAZON_MARKETPLACES
from app.methodology import PHASE_ORDER, SPECS
from app.model_client import ModelClient, ModelConfigError
from app.recovery import CheckpointStore, ErrorStateStore
from app.research_data import (
    CandidateStore,
    EvidenceStore,
    ObservationStore,
    OpportunityStore,
)
from app.research_runner import ResearchRunner, RunnerConfig
from app.reports import ReportStore
from app.runner_registry import RunnerRegistry, RunnerRegistryError
from app.sessions import SessionStore


class FakePage:
    def __init__(self) -> None:
        self.url = "https://www.amazon.com/s?k=test"

    async def title(self) -> str:
        return "Page"


class FakeBrowser:
    def __init__(self) -> None:
        self.page = FakePage()

    async def open_marketplace(self, marketplace: Any, path: str = "/") -> Any:
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
        class A:
            id = "art"
        return A()


class StubAuth:
    async def _classify_page(self, page: Any, marketplace: Any) -> Any:
        from app.amazon_auth import AuthCheck

        return AuthCheck("unknown", page.url, "t", {})


class FakeModelReply:
    def __init__(self, content: dict[str, Any]) -> None:
        self.content = content
        self.model = "fake"
        self.elapsed_seconds = 0.1
        self.raw = "{}"


class ScriptedModel:
    """Returns canned phase outputs; asserts the phase sequence.

    Like the real model, it reads the evidence digest from the prompt and
    cites REAL evidence ids in its analyses (the prompt tells it ids are
    stable and must be referenced), EXPLORES while the deterministic exit
    gate still reports missing new evidence (capture_evidence actions), and
    then COMMITS with no actions once the gate is satisfied — the explore /
    commit split the runner enforces.
    """

    def __init__(self, script: dict[str, list[dict[str, Any]]]) -> None:
        self.script = script
        self.calls: list[str] = []
        self.user_prompts: dict[str, list[str]] = {}
        self.usage = type("U", (), {"calls": 0, "failed_calls": 0})()

    @staticmethod
    def _evidence_required(user: str) -> int:
        """New evidence the phase gate still requires (0 = may commit)."""
        import re

        m = re.search(r'"evidence_required":\s*(\d+)', user)
        return int(m.group(1)) if m else 0

    @staticmethod
    def _cited_ids(user: str) -> list[str]:
        """Evidence ids visible in the digest (what the model can cite)."""
        import re

        return list(dict.fromkeys(re.findall(r'"id":"([0-9a-f]{12,})"', user)))[:4]

    def _reply_for(self, phase: str, user: str) -> FakeModelReply:
        import copy

        queue = self.script.get(phase) or [{}]
        # Deep-copy: script dicts are shared across tests/sessions; mutating
        # them in place would leak ids between sessions.
        content = copy.deepcopy(queue[0])
        ids = self._cited_ids(user)
        if ids:
            # Cite real evidence in every analysis item, like the real model.
            for key, value in content.items():
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict):
                            item.setdefault("evidence_ids", list(ids))
        # Explore only while the exit gate still owes new evidence; once it
        # reports 0 the model must COMMIT (no actions) or the phase keeps
        # looping. Mirrors exactly what the real prompt instructs.
        if phase != "opportunity_synthesis" and self._evidence_required(user) > 0:
            content["actions"] = [
                {"action": "capture_evidence", "args": {"note": f"evidence batch {i}"}}
                for i in range(6)
            ]
        return FakeModelReply(content)

    async def call(self, session_id: str, *, system: str, user: str, **k: Any) -> Any:
        for phase in PHASE_ORDER:
            if SPECS[phase].guidance[:40] in system:
                self.calls.append(phase)
                self.user_prompts.setdefault(phase, []).append(user)
                return self._reply_for(phase, user)
        raise AssertionError("model called with unknown phase prompt")

    async def aclose(self) -> None:
        pass


class RejectAllModel(ScriptedModel):
    """Phase 8 rejects every candidate, whatever the script says.

    Uses LIVE candidate ids, so no candidate can survive to synthesis: the
    session ends each pass with zero verified candidates and the strategic
    focus pivot gate can fire.
    """

    def __init__(self, script: dict[str, list[dict[str, Any]]], db: Database, session_id: str) -> None:
        super().__init__(script)
        self._candidates = CandidateStore(db)
        self._session_id = session_id

    def _reply_for(self, phase: str, user: str) -> FakeModelReply:
        reply = super()._reply_for(phase, user)
        if phase == "adversarial_verification":
            reply.content["verdicts"] = [
                {"candidate_id": c.id, "verdict": "reject", "reasons": ["dead focus"]}
                for c in self._candidates.list(self._session_id, limit=50)
            ]
        return reply


class PausingModel:
    """Simulates an auth pause raised during a browser action."""

    def __init__(self) -> None:
        self.calls = 0

    async def call(self, *a: Any, **k: Any) -> Any:
        self.calls += 1
        return FakeModelReply({
            "signals": [],
            "observations": [],
            "actions": [{"action": "search_marketplace", "args": {"query": "x", "screenshot": False}}],
        })

    async def aclose(self) -> None:
        pass


def make_runner(db: Database, config: Config, model: Any, *, pause_auth: bool = False) -> ResearchRunner:
    events = EventLog(db)
    sessions = SessionStore(db, config)
    agent = AgentStateStore(db, events)
    errors = ErrorStateStore(db)
    checkpoints = CheckpointStore(db)
    candidates = CandidateStore(db)
    evidence = EvidenceStore(db)
    observations = ObservationStore(db)
    opportunities = OpportunityStore(db)
    artifacts = ArtifactStore(db, config)
    be_store = BrowserEvidenceStore(db)
    browser = FakeBrowser()
    from app.trace import ActivityTrace
    from app.autonomy import AutonomySupervisor

    trace = ActivityTrace(db)
    supervisor = AutonomySupervisor(
        db, config, sessions=sessions, events=events,
        errors=errors, checkpoints=checkpoints,
    )
    collector = EvidenceCollector(browser, be_store, FakeArtifacts())
    auth = StubAuth()
    if pause_auth:
        from app.amazon_auth import AuthCheck

        class PausingAuth(StubAuth):
            async def _classify_page(self, page: Any, marketplace: Any) -> Any:
                return AuthCheck("captcha_required", page.url, "t", {"reason": "test"})
        auth = PausingAuth()
    executor = ActionExecutor(config, browser, collector, auth, events)  # type: ignore[arg-type]
    executor.set_marketplaces([AMAZON_MARKETPLACES["us"]])
    return ResearchRunner(
        db, config,
        sessions=sessions, agent=agent, events=events, errors=errors,
        checkpoints=checkpoints, candidates=candidates, evidence=evidence,
        observations=observations, opportunities=opportunities,
        artifacts=artifacts, browser=browser,  # type: ignore[arg-type]
        browser_evidence=be_store,
        browser_auth=type("BAS", (), {})(),
        model=model,  # type: ignore[arg-type]
        runner_config=RunnerConfig(max_model_calls_per_session=100),
        executor=executor,
        trace=trace,
        supervisor=supervisor,
    )


@pytest.fixture()
def session(db, config) -> str:
    with db.tx() as conn:
        pass
    sessions = SessionStore(db, config)
    config.max_concurrent_sessions = 3
    s = sessions.create(mode="prompt", prompt="research grief journals for adults", objective="find niches")
    return s.id


PHASE_OUTPUTS: dict[str, list[dict[str, Any]]] = {
    "opportunity_discovery": [{"signals": [
        {"topic": "grief journals", "audience": "adult children", "marketplace": "us", "source": "search"},
        {"topic": "prayer journals", "audience": "new believers", "marketplace": "us", "source": "category"},
    ], "observations": ["search results show low review depth"]}],
    "aggressive_niching": [{"niches": [
        {"niche": "sudden-loss grief journals for adult children", "parent_market": "grief journals",
         "chain": ["grief", "sudden loss"], "demand_hints": "autocomplete shows sustained variants",
         "marketplace": "us"},
        {"niche": "90-day prayer journals for new believers", "parent_market": "prayer journals",
         "chain": ["prayer"], "demand_hints": "multiple mid-list sellers", "marketplace": "us"},
        {"niche": "guided divorce recovery journals for men in year one", "parent_market": "divorce recovery",
         "chain": ["divorce", "men"], "demand_hints": "rising search variants", "marketplace": "us"},
    ]}],
    "competitive_landscape": [{"competitive_maps": [
        {"niche": "sudden-loss grief journals", "competitors": ["A", "B", "C"], "saturation": "low",
         "pricing": "9.99", "patterns": "generic prompts"},
    ]}],
    "consumer_intelligence": [{"consumer_maps": [
        {"niche": "sudden-loss grief journals", "praises": ["gentle tone"], "complaints": ["no structure"],
         "unmet": ["legal checklist"], "design_opportunities": ["estate section"]},
    ]}],
    "demand_validation": [{"demand_assessments": [
        {"niche": "sudden-loss grief journals", "verdict": "sustained demand", "signals": ["review velocity"],
         "risks": []},
    ]}],
    "opportunity_gap": [{"gap_analyses": [
        {"niche": "sudden-loss grief journals", "exists": "generic journals", "sells": "yes",
         "readers_want": "structure", "competitors_fail": "no estate guidance",
         "better": "estate checklist", "why_choose": "only guided option"},
    ]}],
    "cross_market_validation": [{"cross_market_checks": [
        {"niche": "sudden-loss grief journals", "marketplace": "uk", "verdict": "present", "notes": "fewer titles"},
    ]}],
    "adversarial_verification": [{"verdicts": []}],
    "opportunity_synthesis": [{"opportunities": [{
        "niche": "sudden-loss grief journals for adult children",
        "parent_market": "grief journals",
        "target_reader": "adult children who lost a parent suddenly",
        "reader_problem": "overwhelm",
        "marketplaces": ["us"],
        "evidence_ids": [],
        "competitive_landscape": "3 incumbents",
        "consumer_needs": "structure",
        "market_gap": "estate guidance",
        "differentiation": "estate checklist",
        "risks": ["narrow"],
        "keywords": ["grief journal"],
        "title_concepts": ["Suddenly Gone"],
        "positioning": "the organized grief journal",
        "confidence": 0.7,
        "verification_status": "pass",
    }]}],
}


@pytest.mark.asyncio
async def test_full_methodology_produces_validated_opportunity(db, config, session):
    sessions = SessionStore(db, config)
    model = ScriptedModel(PHASE_OUTPUTS)
    runner = make_runner(db, config, model)
    await runner.run(session)
    s = sessions.require(session)
    assert s.status == "completed", s.error
    opps = OpportunityStore(db).list(session)
    assert len(opps) == 1
    o = opps[0]
    assert "grief" in o.niche
    assert 0.0 <= o.confidence <= 1.0
    assert "estate" in (o.meta.get("differentiation") or "")
    # Phases must appear in methodology order and all 9 must be visited.
    order_index = [PHASE_ORDER.index(p) for p in model.calls]
    assert order_index == sorted(order_index), model.calls
    assert set(model.calls) == set(PHASE_ORDER)
    # final report persisted
    reports = ReportStore(db, config).list(session)
    assert any(r.kind == "final" for r in reports)
    # agent reached done
    assert AgentStateStore(db, EventLog(db)).require(session).state == "done"
    # a surviving verified candidate means no strategic pivot was considered
    assert not any(e.action == "focus_pivot" for e in EventLog(db).tail(session))


@pytest.mark.asyncio
async def test_vision_screenshot_attached_to_every_research_call(db, config, session):
    """The model must ALWAYS see the current screen: once any screenshot
    exists, EVERY subsequent model call — in all 9 phases, including
    adversarial verification and synthesis — receives it. A phase may never
    run blind."""
    class ScreenshotRecordingModel(ScriptedModel):
        def __init__(self, script: dict[str, list[dict[str, Any]]]) -> None:
            super().__init__(script)
            self.shots: list[Any] = []

        async def call(self, session_id: str, *, system: str, user: str, **k: Any) -> Any:
            # The post-research narrative call is not a research call: it
            # runs after every phase and never receives the screen.
            if not user.startswith("SESSION DATA (verified"):
                self.shots.append(k.get("screenshot_png"))
            return await super().call(session_id, system=system, user=user, **k)

    model = ScreenshotRecordingModel(PHASE_OUTPUTS)
    runner = make_runner(db, config, model)
    await runner.run(session)
    assert SessionStore(db, config).require(session).status == "completed"
    shots = model.shots
    assert len(shots) >= len(PHASE_ORDER), f"too few calls: {len(shots)}"
    first = next((i for i, s in enumerate(shots) if s is not None), None)
    assert first is not None, "no screenshot ever reached the model"
    # From the first attached screenshot onward NO call may drop it — the
    # adversarial and synthesis phases (which browse least) included.
    assert all(s is not None for s in shots[first:]), \
        [i for i, s in enumerate(shots) if s is None]


@pytest.mark.asyncio
async def test_activity_notes_are_tagged_for_the_reasoning_strip(db, config, session):
    """The per-iteration activity_note is the only surface the UI has for
    "what the agent is doing right now" (chain-of-thought is deliberately
    never recorded), so it must be distinguishable from structural trace
    lines — the "agent now" strip renders exactly these."""
    from app.trace import ActivityTrace

    class NoteModel(ScriptedModel):
        async def call(self, session_id, *, system, user, **k):
            reply = await super().call(session_id, system=system, user=user, **k)
            reply.content["activity_note"] = (
                "Opening competitor product pages to read review patterns"
            )
            return reply

    runner = make_runner(db, config, NoteModel(PHASE_OUTPUTS))
    await runner.run(session)
    assert SessionStore(db, config).require(session).status == "completed"
    entries = ActivityTrace(db).recent(session, limit=200)
    notes = [e for e in entries if (e.detail or {}).get("source") == "activity_note"]
    assert notes, "no activity note reached the trace"
    assert all(e.to_dict()["detail"]["source"] == "activity_note" for e in notes)
    # Structural trace lines (phases, candidates) stay untagged.
    untagged = [e for e in entries if not (e.detail or {}).get("source")]
    assert untagged, "structural trace lines lost their shape"


@pytest.mark.asyncio
async def test_model_usage_checkpointed_per_session(db, config, session):
    """Token/cost usage is persisted per session after every model call —
    the durable source behind the session's usage display."""
    from types import SimpleNamespace

    class UsageModel(ScriptedModel):
        def __init__(self, script: dict[str, list[dict[str, Any]]]) -> None:
            super().__init__(script)
            self.n = 0

        async def call(self, session_id: str, *, system: str, user: str, **k: Any) -> Any:
            reply = await super().call(session_id, system=system, user=user, **k)
            self.n += 1
            # Cumulative for the run — mirrors the real ModelClient.usage.
            reply.usage = SimpleNamespace(
                calls=self.n, failed_calls=0,
                prompt_tokens=1000 * self.n, completion_tokens=100 * self.n,
                cost_usd=0.001 * self.n,
            )
            return reply

    model = UsageModel(PHASE_OUTPUTS)
    runner = make_runner(db, config, model)
    await runner.run(session)
    assert SessionStore(db, config).require(session).status == "completed"
    cp = CheckpointStore(db).get(session, "model_usage")
    assert cp is not None, "usage checkpoint missing"
    p = cp.payload
    assert p["calls"] == model.n and model.n > 0
    assert p["prompt_tokens"] == 1000 * model.n
    assert p["completion_tokens"] == 100 * model.n
    assert p["cost_usd"] == pytest.approx(0.001 * model.n, abs=1e-6)


@pytest.mark.asyncio
async def test_phase_sequence_enforced_even_when_model_hides_phases(db, config, session):
    """The scripted model can't skip phases: prompts carry the phase contract."""
    model = ScriptedModel(PHASE_OUTPUTS)
    runner = make_runner(db, config, model)
    await runner.run(session)
    sessions = SessionStore(db, config)
    assert sessions.require(session).status == "completed"
    # all phases visited, in methodology order (a phase may need >1 iteration)
    order_index = [PHASE_ORDER.index(p) for p in model.calls]
    assert order_index == sorted(order_index), model.calls
    assert set(model.calls) == set(PHASE_ORDER)


@pytest.mark.asyncio
async def test_auth_pause_lands_in_durable_paused_status(db, config, session):
    sessions = SessionStore(db, config)
    model = PausingModel()
    runner = make_runner(db, config, model, pause_auth=True)
    await runner.run(session)
    s = sessions.require(session)
    assert s.status == "paused"
    assert "authentication" in (s.error or "")
    events = EventLog(db)
    assert any(e.action == "paused_for_auth" for e in events.tail(session))
    # checkpoint saved for resumability
    cp = CheckpointStore(db).get(session, "paused:auth")
    assert cp is not None


# =====================================================================
# Self-heal: an unexpected error recovers; it never kills the session
# =====================================================================
@pytest.mark.asyncio
async def test_self_heal_recovers_transient_error_without_failing(db, config, session):
    """A mid-run crash (like the hallucinated candidate id that failed a
    live session at Verification) must heal from the durable checkpoint
    cursor: the research COMPLETES, the heal is auditable on the event
    feed, and no research_failed event is ever emitted."""
    sessions = SessionStore(db, config)
    runner = make_runner(db, config, ScriptedModel(PHASE_OUTPUTS))
    calls = {"n": 0}
    orig = runner._refresh_whitespace

    def flaky(sid: str):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("candidate cand_true_facts_imprint not found in session x")
        return orig(sid)

    runner._refresh_whitespace = flaky  # type: ignore[method-assign]
    await runner.run(session)

    s = sessions.require(session)
    assert s.status == "completed", s.error
    assert calls["n"] >= 2, "the flaky call was never retried"
    actions = [e.action for e in EventLog(db).tail(session)]
    assert "research_self_heal" in actions, actions
    assert "research_failed" not in actions, actions
    # The heal is durable: a recoverable error state + a checkpoint record.
    assert CheckpointStore(db).get(session, "self_heal") is not None


def test_unknown_verdict_candidate_id_is_skipped_not_fatal(db, config, session):
    """The model may emit a hallucinated candidate id in an adversarial
    verdict — the exact crash that failed a live session mid-verification.
    It must be skipped with a durable warning while real verdicts still
    apply."""
    runner = make_runner(db, config, ScriptedModel(PHASE_OUTPUTS))
    real = runner.candidates.create(session, niche="real niche", marketplace="us")
    runner._apply_phase_effects(
        session, "adversarial_verification",
        {
            "verdicts": [
                {"candidate_id": "cand_true_facts_imprint", "verdict": "pass"},
                {"candidate_id": real.id, "verdict": "reject", "reasons": ["weak evidence"]},
            ]
        },
    )
    # Unknown id skipped; the real candidate's verdict still lands.
    assert runner.candidates.get(session, real.id).status == "rejected"
    actions = [e.action for e in EventLog(db).tail(session)]
    assert "verdict_skipped_unknown_candidate" in actions, actions


def test_string_niches_still_create_candidates(db, config, session):
    """Accounting bug: when the model emitted niches as plain strings the
    dict-only filter silently created ZERO candidates — a productive session
    then showed '0 candidates' and failed the candidate gate."""
    runner = make_runner(db, config, ScriptedModel(PHASE_OUTPUTS))
    runner._apply_phase_effects(
        session, "aggressive_niching",
        {"niches": ["swimmer journals", "triathlete logbooks",
                    {"niche": "dict niche", "marketplace": "us"}, "  "]},
    )
    cands = runner.candidates.list(session)
    niches = sorted(c.niche for c in cands)
    assert niches == ["dict niche", "swimmer journals", "triathlete logbooks"]


def test_verdict_with_unknown_id_falls_back_to_its_niche(db, config, session):
    """A verdict about a REAL candidate must land even when the model
    invented the candidate id: match the niche, correct the id durably,
    and only skip when neither id nor niche resolves to a real row."""
    runner = make_runner(db, config, ScriptedModel(PHASE_OUTPUTS))
    real = runner.candidates.create(session, niche="ocean fact books", marketplace="uk")
    orphan = runner.candidates.create(session, niche="never named", marketplace="us")
    runner._apply_phase_effects(
        session, "adversarial_verification",
        {
            "verdicts": [
                {"candidate_id": "cand_invented_123", "niche": "Ocean Fact Books",
                 "verdict": "pass"},
                {"candidate_id": "cand_also_invented", "niche": "a niche nobody has",
                 "verdict": "reject"},
            ]
        },
    )
    assert runner.candidates.get(session, real.id).status == "verified"
    assert runner.candidates.get(session, orphan.id).status == "discovered"
    actions = [e.action for e in EventLog(db).tail(session)]
    assert "verdict_id_corrected_by_niche" in actions, actions
    assert "verdict_skipped_unknown_candidate" in actions, actions


# =====================================================================
# Resume: the agent state machine must let a failed run start again
# =====================================================================
@pytest.mark.asyncio
async def test_resume_from_failed_agent_state_completes(db, config, session):
    """Regression: _fail() parks the agent in the TERMINAL 'failed' state
    (no outgoing edges). Resuming then raised AgentStateError —
    'invalid transition failed -> planning; allowed: []' — on the runner's
    very first transition, the self-heal budget burned on the same
    unstoppable error, and the session re-failed. Exactly what the owner
    hit tapping Resume."""
    sessions = SessionStore(db, config)
    agent = AgentStateStore(db, EventLog(db))
    sessions.update(session, status="failed", error="candidate x not found")
    agent.initialize(session)
    agent.transition(session, "planning", substate="methodology_start")
    agent.transition(session, "failed", substate="runner_error")
    # The resume route marks the session queued before spawning the runner.
    sessions.update(session, status="queued")

    runner = make_runner(db, config, ScriptedModel(PHASE_OUTPUTS))
    await runner.run(session)

    s = sessions.require(session)
    assert s.status == "completed", s.error
    assert agent.require(session).state == "done"


@pytest.mark.asyncio
async def test_resume_skips_straight_to_report_when_all_phases_checkpointed(db, config, session):
    """A run that died with every phase already committed (e.g. while
    writing the report) resumes by skipping ALL phases — planning must be
    able to jump straight to reporting or the resume re-fails. Nothing
    researched is re-run: the resumed model is never called for a phase."""
    sessions = SessionStore(db, config)
    agent = AgentStateStore(db, EventLog(db))
    await make_runner(db, config, ScriptedModel(PHASE_OUTPUTS)).run(session)
    assert sessions.require(session).status == "completed"

    # Emulate a crash at report time: the durable marks a failed run leaves.
    agent.transition(session, "planning", substate="crash_setup")
    agent.transition(session, "failed", substate="runner_error")
    sessions.update(session, status="failed", error="report write crashed")
    sessions.update(session, status="queued")

    resumed_model = ScriptedModel(PHASE_OUTPUTS)
    await make_runner(db, config, resumed_model).run(session)

    s = sessions.require(session)
    assert s.status == "completed", s.error
    assert agent.require(session).state == "done"
    # Every phase_output checkpoint was reused: zero phase re-runs.
    assert resumed_model.calls == [], resumed_model.calls


@pytest.mark.asyncio
async def test_run_shows_live_phase_and_accumulates_elapsed(db, config, session):
    """Two truths the owner was confused by: the session row must show the
    phase that is RUNNING (it previously stayed 'initializing' until phase 1
    finished while the event feed already said "Entering Phase 1"), and
    elapsed_seconds must actually accumulate so the sessions card doesn't
    read 0:00 while the detail view ticks."""
    sessions = SessionStore(db, config)
    model = ScriptedModel(PHASE_OUTPUTS)
    seen: list[str] = []
    orig_call = model.call

    async def spy(*a: Any, **k: Any) -> Any:
        seen.append(sessions.require(session).phase)
        return await orig_call(*a, **k)

    model.call = spy  # type: ignore[method-assign]
    runner = make_runner(db, config, model)
    await runner.run(session)
    s = sessions.require(session)
    assert "discovery" in seen  # visible WHILE phase 1 runs, not after
    assert s.status == "completed"
    assert s.phase == "synthesis"  # final mapped phase — not stuck at initializing
    assert s.elapsed_seconds > 0  # the list card reads this directly


@pytest.mark.asyncio
async def test_unconfigured_model_pauses_session(db, config, session):
    sessions = SessionStore(db, config)

    class Unconfigured:
        is_configured = False

        async def call(self, *a: Any, **k: Any) -> Any:
            raise ModelConfigError("MODEL_API_KEY is not set")

        async def aclose(self) -> None:
            pass

    runner = make_runner(db, config, Unconfigured())
    await runner.run(session)
    s = sessions.require(session)
    assert s.status == "paused"
    assert "model" in (s.error or "").lower()


@pytest.mark.asyncio
async def test_cancel_requested_before_run_marks_cancelled(db, config, session):
    sessions = SessionStore(db, config)
    model = ScriptedModel(PHASE_OUTPUTS)

    class CancellableModel(ScriptedModel):
        pass

    runner = make_runner(db, config, model)
    runner.cancel()
    await runner.run(session)
    assert sessions.require(session).status == "cancelled"


@pytest.mark.asyncio
async def test_registry_enforces_concurrency(db, config, session):
    config.max_concurrent_sessions = 1
    events = EventLog(db)
    errors = ErrorStateStore(db)
    registry = RunnerRegistry(
        db, config, sessions=SessionStore(db, config), events=events, errors=errors,
        state=type("S", (), {
            "sessions": SessionStore(db, config),
            "agent": AgentStateStore(db, events),
            "events": events,
            "errors": errors,
            "checkpoints": CheckpointStore(db),
            "candidates": CandidateStore(db),
            "evidence": EvidenceStore(db),
            "observations": ObservationStore(db),
            "opportunities": OpportunityStore(db),
            "artifacts": ArtifactStore(db, config),
            "browser_manager": FakeBrowser(),
            "browser_evidence": BrowserEvidenceStore(db),
            "browser_auth": type("BAS", (), {})(),
            "model": None,
        })(),
    )
    # Patch the builder to avoid real ModelClient/browser wiring.
    registry._build_runner = lambda _sid: make_runner(db, config, ScriptedModel(PHASE_OUTPUTS))  # type: ignore[assignment]
    # The API route marks the session queued before spawning a runner.
    SessionStore(db, config).update(session, status="queued")
    registry.start(session)
    assert registry.active_count() == 1
    assert registry.is_running(session)
    st = registry.status()
    assert st["limit"] == 1
    assert session in st["sessions"]
    # Let the runner finish; the registry must clean up its tracking.
    for _ in range(100):
        await asyncio.sleep(0.05)
        if not registry.is_running(session):
            break
    assert registry.active_count() == 0
    assert SessionStore(db, config).require(session).status == "completed"


@pytest.mark.asyncio
async def test_registry_refuses_runnable_only_statuses(db, config):
    events = EventLog(db)
    errors = ErrorStateStore(db)
    sessions = SessionStore(db, config)
    registry = RunnerRegistry(
        db, config, sessions=sessions, events=events, errors=errors, state=type("S", (), {})(),
    )
    with pytest.raises(Exception):
        registry.start("no-such-session")


# =====================================================================
# Autonomy layer: pause, timing, trace, supervisor recovery
# =====================================================================
class TestOwnerPause:
    @pytest.mark.asyncio
    async def test_pause_at_safe_boundary_lands_durable(self, db, config, session):
        """request_pause() must stop the run at a safe boundary with a
        durable paused status and a resume checkpoint — never mid-write."""
        sessions = SessionStore(db, config)
        model = ScriptedModel(PHASE_OUTPUTS)
        runner = make_runner(db, config, model)
        # Pause after the first model call of the first phase.
        original_call = model.call

        async def call_and_pause(*a, **k):
            reply = await original_call(*a, **k)
            runner.request_pause()
            return reply

        model.call = call_and_pause  # type: ignore[method-assign]
        await runner.run(session)
        s = sessions.require(session)
        assert s.status == "paused", s.error
        cp = CheckpointStore(db).get(session, "paused:owner")
        assert cp is not None
        # Resume: the runner continues and completes.
        runner2 = make_runner(db, config, ScriptedModel(PHASE_OUTPUTS))
        await runner2.run(session)
        assert sessions.require(session).status == "completed"


class TestTimingAndStuck:
    def test_timeouts_are_task_type_specific(self):
        from app.timing import TaskType, timeout_for

        assert timeout_for(TaskType.MODEL_CALL)["hard"] > timeout_for(TaskType.PAGE_NAVIGATION)["hard"]
        assert timeout_for(TaskType.PHASE)["hard"] > timeout_for(TaskType.MODEL_CALL)["hard"]

    def test_stuck_verdict_escalates(self, db, session):
        from app.timing import TaskType, TimingTracker

        t = TimingTracker(db, session)
        t.start("t1", TaskType.TASK)
        assert t.check("t1").level == "ok"
        # Fake a long-running task by backdating the start row.
        with db.tx() as conn:
            conn.execute(
                "UPDATE task_timings SET started_at = ? WHERE task_key = 't1'",
                ("2000-01-01T00:00:00+00:00",),
            )
        t2 = TimingTracker(db, session)
        t2._running["t1"] = (TaskType.TASK, __import__("time").monotonic() - 10_000, 0.0)
        v = t2.check("t1")
        assert v.level == "hard"
        assert v.hard_stuck
        t2.abandon("t1")
        t.abandon("t1")

    def test_timing_summary_persisted(self, db, session):
        from app.timing import TaskType, TimingTracker

        tracker = TimingTracker(db, session)
        tracker.start("x1", TaskType.MODEL_CALL)
        tracker.finish("x1", outcome="ok")
        s = tracker.session_summary()
        assert s["by_type"][TaskType.MODEL_CALL]["count"] == 1
        assert s["slowest"][0]["task_key"] == "x1"


@pytest.mark.asyncio
async def test_runner_records_task_timings_and_trace(db, config, session):
    from app.timing import TimingTracker
    from app.trace import ActivityTrace

    runner = make_runner(db, config, ScriptedModel(PHASE_OUTPUTS))
    await runner.run(session)
    tracker = TimingTracker(db, session)
    summary = tracker.session_summary()
    assert summary["by_type"].get("phase"), summary
    trace = ActivityTrace(db)
    entries = trace.recent(session, limit=100)
    texts = " ".join(e.text for e in entries)
    assert "Entering" in texts
    assert "Candidate created" in texts
    assert "Verification" in texts


# =====================================================================
class TestAutonomySupervisor:
    def _supervisor(self, db, config):
        from app.autonomy import AutonomySupervisor

        return AutonomySupervisor(
            db, config, sessions=SessionStore(db, config),
            events=EventLog(db), errors=ErrorStateStore(db),
            checkpoints=CheckpointStore(db),
        )

    def test_recover_on_boot_resumes_checkpointed_sessions(self, db, config, session):
        """Restart -> checkpointed sessions become interrupted + resume-queued."""
        sup = self._supervisor(db, config)
        CheckpointStore(db).save(session, "phase_complete:opportunity_discovery", {"at": "now"})
        SessionStore(db, config).update(session, status="running")
        counts = sup.recover_on_boot()
        assert counts["resumed"] == 1
        assert SessionStore(db, config).require(session).status == "interrupted"
        assert CheckpointStore(db).get(session, "resume_requested") is not None

    def test_recover_on_boot_keeps_pause_checkpoints_paused(self, db, config, session):
        sup = self._supervisor(db, config)
        CheckpointStore(db).save(session, "paused:auth", {"at": "now"})
        SessionStore(db, config).update(session, status="running")
        counts = sup.recover_on_boot()
        assert counts["waiting"] == 1
        assert SessionStore(db, config).require(session).status == "paused"

    def test_wake_never_resumes_a_finished_research(self, db, config, session):
        """A session whose final report is durable is FINISHED, even when the
        status column is stale ('running') because the process died around
        the final update. Wake/reboot must correct the status to completed
        and must never auto-resume the finished research."""
        from app.reports import ReportStore

        sup = self._supervisor(db, config)
        sessions = SessionStore(db, config)
        CheckpointStore(db).save(
            session, "phase_complete:opportunity_synthesis", {"at": "now"}
        )
        ReportStore(db, config).save(
            session, kind="final", title="Final Report", body_markdown="# done",
        )
        sessions.update(session, status="running")

        counts = sup.recover_on_boot()
        assert counts["finished"] == 1
        assert counts["resumed"] == 0
        assert sessions.require(session).status == "completed"

        # Even with a stale resume request + interrupted status, the finished
        # research must not respawn a runner.
        CheckpointStore(db).save(session, "resume_requested", {"at": "now"})
        sessions.update(session, status="interrupted")

        class _NoStart:
            def start(self, sid):
                raise AssertionError("finished research must never respawn a runner")

        assert sup.auto_resume_recovered(_NoStart()) == 0

    def test_job_states_flow_through_supervisor(self, db, config, session):
        from app.autonomy import JOB_RESEARCHING, JOB_WAITING_AUTH

        sup = self._supervisor(db, config)
        st = requestless_job(db, config, session)
        sup.mark(session, JOB_RESEARCHING)
        job = sup.current_job(session)
        assert job is not None and job.status == JOB_RESEARCHING
        sup.mark(session, JOB_WAITING_AUTH)
        assert sup.current_job(session).status == JOB_WAITING_AUTH

    def test_heartbeat_updates_checkpoint(self, db, config, session):
        sup = self._supervisor(db, config)
        sup.heartbeat(session, phase="discovery", note="mid-phase")
        cp = CheckpointStore(db).get(session, "heartbeat")
        assert cp is not None and cp.payload["phase"] == "discovery"


def requestless_job(db, config, session_id):
    """Create a research job row directly (route-less helper)."""
    return JobOrchestrator(db, config, SessionStore(db, config), EventLog(db), ErrorStateStore(db)).create_job(
        session_id, kind="research_run", payload={}
    )


def test_runner_config_defaults_are_depth_friendly():
    rc = RunnerConfig()
    assert rc.max_model_calls_per_session >= 100
    assert rc.max_opportunities >= 10


# ============================================ exit-gate integrity (regression)
# The three bugs behind a rich-evidence session ending with ZERO candidates:
# a session-wide evidence floor, phases committing on their explore step, and
# budget exhaustion discarding the model's work.

class ImmediateCommitModel(ScriptedModel):
    """Never requests an action: tries to commit on its very first reply."""

    def _reply_for(self, phase: str, user: str) -> FakeModelReply:
        reply = super()._reply_for(phase, user)
        reply.content.pop("actions", None)
        return reply


class NeverCommitModel(ScriptedModel):
    """Always explores — it never stops asking for actions."""

    def _reply_for(self, phase: str, user: str) -> FakeModelReply:
        reply = super()._reply_for(phase, user)
        reply.content["actions"] = [
            {"action": "capture_evidence", "args": {"note": "again"}} for _ in range(2)
        ]
        return reply


def _seed_evidence(db, session_id: str, n: int) -> None:
    store = BrowserEvidenceStore(db)
    for i in range(n):
        store.create(
            session_id=session_id, marketplace="us", kind="search_results",
            url=f"https://www.amazon.com/s?k=old{i}",
            title=f"Amazon.com : legacy query {i}", data={},
        )


@pytest.mark.asyncio
async def test_evidence_floor_counts_phase_new_not_session_total(db, config, session):
    """A phase may not borrow earlier phases' evidence to open its exit gate.

    Before the fix the gate read the SESSION total, so 100 records captured in
    Phase 1 satisfied every later floor instantly and a phase could commit on
    its first iteration with nothing researched.
    """
    sessions = SessionStore(db, config)
    _seed_evidence(db, session, 40)  # legacy session-wide evidence
    model = ScriptedModel(PHASE_OUTPUTS)
    runner = make_runner(db, config, model)
    await runner.run(session)
    assert sessions.require(session).status == "completed"
    # The gate must demand THIS phase's evidence even though 40 records from
    # earlier phases already exist: with the old session-wide count the
    # requirement read 0 and Phase 1 committed on its first reply.
    first_p1 = model.user_prompts["opportunity_discovery"][0]
    # Depth-raised while the session is under RESEARCH_MIN_HOURS: the gate
    # asks for the declared floor times DEPTH_EVIDENCE_MULTIPLIER, so spare
    # hours turn into deeper research instead of an early finish.
    assert ScriptedModel._evidence_required(first_p1) == (
        SPECS["opportunity_discovery"].min_evidence
        * config.depth_evidence_multiplier
    )
    assert len(model.user_prompts["opportunity_discovery"]) > 1, model.calls
    events = EventLog(db).tail(session)
    p1 = [e for e in events if e.action == "phase_completed"
          and e.detail["phase"] == "opportunity_discovery"]
    assert p1, "phase 1 never completed"
    assert p1[0].detail["evidence_new"] >= SPECS["opportunity_discovery"].min_evidence


@pytest.mark.asyncio
async def test_phase_commits_only_without_pending_actions(db, config, session):
    """An explore step (reply with actions) never closes the phase — only a
    commit (no actions, floors met) does."""
    sessions = SessionStore(db, config)
    model = ScriptedModel(PHASE_OUTPUTS)
    runner = make_runner(db, config, model)
    await runner.run(session)
    assert sessions.require(session).status == "completed"
    events = [e for e in EventLog(db).tail(session) if e.action == "phase_completed"]
    assert {e.detail["phase"] for e in events} == set(PHASE_ORDER)
    # Every commit happened on an iteration that requested no actions: the
    # synthetic model only stops requesting actions once the gate reads 0.
    for phase, prompts in model.user_prompts.items():
        if phase == "opportunity_synthesis":
            continue
        last = prompts[-1]
        # A phase may close on its DECLARED floors when it is stuck deepening:
        # no new distinct source for STALL_LIMIT rounds, but every declared
        # requirement met. That commit is recorded as `phase_committed_at_
        # depth_ceiling`, so the raised floor can never become a spin loop.
        if ScriptedModel._evidence_required(last) != 0:
            ceilings = [
                e for e in EventLog(db).tail(session)
                if e.action == "phase_committed_at_depth_ceiling"
                and e.detail.get("phase") == phase
            ]
            assert ceilings, (
                f"{phase} closed without meeting its gate and without a "
                "depth-ceiling record"
            )


@pytest.mark.asyncio
async def test_duplicate_captures_do_not_reset_the_stall_guard(db, config, session):
    """Re-running the identical search must read as no progress, so the
    anti-stall guard ends a phase that is busy but not going deeper — instead
    of looping to its full budget."""
    from app.methodology import STALL_LIMIT

    sessions = SessionStore(db, config)
    model = NeverCommitModel(PHASE_OUTPUTS)
    runner = make_runner(db, config, model)
    await runner.run(session)
    assert sessions.require(session).status == "completed"
    assert any(e.action == "phase_stalled" for e in EventLog(db).tail(session))
    p1_calls = model.calls.count("opportunity_discovery")
    assert p1_calls < SPECS["opportunity_discovery"].max_iterations
    assert p1_calls <= STALL_LIMIT + 1


@pytest.mark.asyncio
async def test_budget_exhaustion_still_applies_the_model_output(db, config, session):
    """A phase that burns its whole budget must contribute what the model
    produced (niches → candidates), never return {} and drop it."""
    sessions = SessionStore(db, config)
    model = NeverCommitModel(PHASE_OUTPUTS)
    runner = make_runner(db, config, model)
    await runner.run(session)
    assert sessions.require(session).status == "completed"
    events = EventLog(db).tail(session)
    assert any(e.action == "phase_completed_from_budget" for e in events)
    # The niching output reached the candidate ledger despite no clean commit.
    cands = CandidateStore(db).list(session, limit=50)
    assert any("grief" in c.niche for c in cands)


@pytest.mark.asyncio
async def test_empty_niching_output_cannot_commit_while_signals_exist(db, config, session):
    """Phase 2 is where candidates are BORN: with Phase 1 signals on the
    table it may not commit an empty niches list (the zero-candidate bug)."""
    from app.methodology import P1_DISCOVERY, P2_NICHING

    class EmptyNichingModel(ScriptedModel):
        def _reply_for(self, phase: str, user: str) -> FakeModelReply:
            reply = super()._reply_for(phase, user)
            if phase == P2_NICHING:
                reply.content["niches"] = []
                reply.content.pop("actions", None)  # tries to commit empty
            return reply

    sessions = SessionStore(db, config)
    model = EmptyNichingModel(PHASE_OUTPUTS)
    runner = make_runner(db, config, model)
    await runner.run(session)
    assert sessions.require(session).status == "completed"
    p2_calls = model.calls.count(P2_NICHING)
    # It may not commit on the first attempt: the output floor keeps the
    # phase open until the stall guard forces it to hand over what it has.
    assert p2_calls > 1, model.calls
    assert not any(
        e.action == "phase_completed" and e.detail["phase"] == P2_NICHING
        for e in EventLog(db).tail(session)
    )


# ------------------------------------------------------------ focus pivot
@pytest.mark.asyncio
async def test_strategic_pivot_reruns_discovery_on_new_focus(db, config, session):
    """Deep research kills every candidate -> the session pivots to a new
    focus (P1 re-runs with the pivot directive) instead of ending."""
    from app.trace import ActivityTrace

    sessions = SessionStore(db, config)
    events = EventLog(db)
    trace = ActivityTrace(db)
    model = RejectAllModel(PHASE_OUTPUTS, db, session)
    runner = make_runner(db, config, model)
    await runner.run(session)
    s = sessions.require(session)
    assert s.status == "completed", s.error

    # one pivot only (budget): synthesis ran exactly once — the pivot gate
    # fires before pass 1 could reach it, and pass 2 completes the report
    assert len(model.user_prompts["opportunity_synthesis"]) == 1

    # the pivot is durable: event + trace + checkpoint
    pivot_events = [e for e in events.tail(session) if e.action == "focus_pivot"]
    assert len(pivot_events) == 1
    assert pivot_events[0].detail["round"] == 1
    pivot_trace = [
        t for t in trace.recent(session)
        if t.kind == "pivot" and t.text.startswith("Strategic pivot (round 1)")
    ]
    assert len(pivot_trace) == 1
    cp = CheckpointStore(db).get(session, "focus_pivots")
    assert cp is not None and len(cp.payload["pivots"]) == 1

    # the directive framed the re-run pass only, and names the dead focus
    p1_prompts = model.user_prompts["opportunity_discovery"]
    plain = [u for u in p1_prompts if "STRATEGIC PIVOT" not in u]
    pivoted = [u for u in p1_prompts if "STRATEGIC PIVOT" in u]
    assert plain and pivoted
    assert p1_prompts.index(plain[-1]) < p1_prompts.index(pivoted[0])
    assert "sudden-loss grief journals for adult children" in pivoted[0]

    # the final report records the pivot
    store = ReportStore(db, config)
    final = [r for r in store.list(session) if r.kind == "final"]
    assert final
    body = store.read_markdown(final[0])
    assert "Strategic pivot (round 1)" in body


@pytest.mark.asyncio
async def test_strategic_pivot_disabled_by_budget(db, config, session):
    """MAX_FOCUS_PIVOTS=0 keeps the old behavior: one pass, then report."""
    config.max_focus_pivots = 0
    events = EventLog(db)
    model = RejectAllModel(PHASE_OUTPUTS, db, session)
    runner = make_runner(db, config, model)
    await runner.run(session)
    assert SessionStore(db, config).require(session).status == "completed"
    # no prompt ever carried pivot framing — a pivot pass is exactly that
    assert all(
        "STRATEGIC PIVOT" not in u
        for prompts in model.user_prompts.values() for u in prompts
    )
    assert not any(e.action == "focus_pivot" for e in events.tail(session))
    assert CheckpointStore(db).get(session, "focus_pivots") is None


@pytest.mark.asyncio
async def test_strategic_pivot_skipped_without_time_budget(db, config, session):
    """A pivot spends a whole new pass: it is skipped (and logged) when too
    little of the session's hard time budget remains."""
    events = EventLog(db)
    model = RejectAllModel(PHASE_OUTPUTS, db, session)
    runner = make_runner(db, config, model)
    runner.rc.min_remaining_session_fraction_for_pivot = 1.5  # never enough
    await runner.run(session)
    assert SessionStore(db, config).require(session).status == "completed"
    assert all(
        "STRATEGIC PIVOT" not in u
        for prompts in model.user_prompts.values() for u in prompts
    )
    assert any(
        e.action == "focus_pivot_skipped_no_time_budget" for e in events.tail(session)
    )
    assert not any(e.action == "focus_pivot" for e in events.tail(session))


def test_focus_pivot_directive_scope():
    from app.methodology import focus_pivot_directive

    brief = focus_pivot_directive(
        round_no=1, dead_niches=["grief journals"], unresolved_niches=[],
        has_brief=True,
    )
    assert "ZERO verified" in brief
    assert "owner's brief remains authoritative" in brief
    assert "grief journals" in brief
    auto = focus_pivot_directive(
        round_no=2, dead_niches=[], unresolved_niches=["prayer journals"],
        has_brief=False,
    )
    assert "no topic was specified" in auto
    assert "prayer journals" in auto


def test_build_user_prompt_includes_pivot_directive():
    from app.methodology import P1_DISCOVERY, PhaseTiming
    from app.prompts import build_user_prompt

    base = dict(
        mode="auto", objective=None, prompt_text="", marketplaces=["us"],
        marketplace_mode="auto", evidence_digest=[], candidates_digest=[],
        prior_phase_outputs=None, timing=PhaseTiming(P1_DISCOVERY, "t0"),
    )
    assert "STRATEGIC PIVOT" not in build_user_prompt(P1_DISCOVERY, **base)
    pivoted = build_user_prompt(
        P1_DISCOVERY, **base, pivot_directive="explore a different parent market"
    )
    assert "STRATEGIC PIVOT" in pivoted
    assert "explore a different parent market" in pivoted


# ============================================ live view + sign-in auto-resume


class _FakeLiveView:
    def __init__(self) -> None:
        self.puts: list[tuple[str, bytes]] = []

    def put(self, sid: str, jpeg: bytes, *, url: str = "", title: str = "") -> None:
        self.puts.append((sid, jpeg))


@pytest.mark.asyncio
async def test_research_screenshots_feed_live_view(db, config, session, monkeypatch):
    """Regression: the runner built its EvidenceCollector WITHOUT the
    live-view store, so the LIVE tab never received a single frame while
    research ran — the owner could only watch manual recordings."""
    import app.live_view as lv_mod
    from app.autonomy import AutonomySupervisor
    from app.trace import ActivityTrace

    events = EventLog(db)
    sessions = SessionStore(db, config)
    supervisor = AutonomySupervisor(
        db, config, sessions=sessions, events=events,
        errors=ErrorStateStore(db), checkpoints=CheckpointStore(db),
    )
    lv = _FakeLiveView()
    runner = ResearchRunner(
        db, config,
        sessions=sessions, agent=AgentStateStore(db, events), events=events,
        errors=ErrorStateStore(db), checkpoints=CheckpointStore(db),
        candidates=CandidateStore(db), evidence=EvidenceStore(db),
        observations=ObservationStore(db), opportunities=OpportunityStore(db),
        artifacts=ArtifactStore(db, config), browser=FakeBrowser(),  # type: ignore[arg-type]
        browser_evidence=BrowserEvidenceStore(db),
        browser_auth=type("BAS", (), {})(),
        model=ScriptedModel(PHASE_OUTPUTS),  # type: ignore[arg-type]
        runner_config=RunnerConfig(),
        trace=ActivityTrace(db),
        supervisor=supervisor,
        live_view=lv,
        live_session_id=session,
    )
    collector = runner.executor.collector
    assert collector._live_view is lv
    assert collector._live_session() == session

    # An actual research screenshot must push a frame FOR THIS SESSION.
    monkeypatch.setattr(lv_mod, "downscale_jpeg", lambda png: b"jpeg-frame")
    await collector.capture_page_state(runner.browser.page, session_id=session, note="n")
    assert lv.puts, "the live-view ring received no frame"
    assert lv.puts[0][0] == session


def test_registry_wires_live_view_into_runner(db, config, monkeypatch):
    """The production construction path (RunnerRegistry._build_runner) must
    pass the live-view store, the thought store (the owner's chain of
    thought), AND the runner's own session id through."""
    import app.model_client as mc

    sentinel = object()
    thought_sentinel = object()
    events = EventLog(db)
    errors = ErrorStateStore(db)
    state = type("S", (), {
        "sessions": SessionStore(db, config),
        "agent": AgentStateStore(db, events),
        "events": events,
        "errors": errors,
        "checkpoints": CheckpointStore(db),
        "candidates": CandidateStore(db),
        "evidence": EvidenceStore(db),
        "observations": ObservationStore(db),
        "opportunities": OpportunityStore(db),
        "artifacts": ArtifactStore(db, config),
        "browser_manager": FakeBrowser(),
        "browser_evidence": BrowserEvidenceStore(db),
        "browser_auth": type("BAS", (), {})(),
        "live_view": sentinel,
        "thoughts": thought_sentinel,
    })()
    registry = RunnerRegistry(
        db, config, sessions=SessionStore(db, config), events=events,
        errors=errors, state=state,
    )
    seen: dict = {}

    def fake_client(cfg, ev, thoughts=None):
        seen["thoughts"] = thoughts
        return ScriptedModel(PHASE_OUTPUTS)

    monkeypatch.setattr(mc, "ModelClient", fake_client)
    runner = registry._build_runner("sess-live-42")
    collector = runner.executor.collector
    assert collector._live_view is sentinel
    assert collector._live_session() == "sess-live-42"
    # The thought store reaches both the client that WRITES reasoning and
    # the runner that upgrades each summary to the model's activity_note.
    assert seen["thoughts"] is thought_sentinel
    assert runner.thoughts is thought_sentinel


def test_resume_auth_paused_sessions_only_touches_auth_pauses():
    """The sign-in hook resumes ONLY sessions waiting on authentication —
    owner pauses, model pauses and running sessions stay untouched, and one
    failing session never blocks the rest."""
    from types import SimpleNamespace

    from app.autonomy import resume_auth_paused_sessions

    class Sess:
        def __init__(self, sid: str, status: str, error: str | None) -> None:
            self.id = sid
            self.status = status
            self.error = error

    auth_paused = Sess(
        "a", "paused",
        "authentication required: sign-in wall (login_required) on amazon.co.uk",
    )
    owner_paused = Sess("b", "paused", "paused by owner")
    running = Sess("c", "running", None)

    class Sessions:
        def __init__(self) -> None:
            self.bumped: list[str] = []
            self.updated: list[tuple[str, dict]] = []

        def list(self, include_terminal: bool = False):
            return [auth_paused, owner_paused, running]

        def bump_resume(self, sid: str) -> None:
            self.bumped.append(sid)

        def update(self, sid: str, **kw: Any) -> None:
            self.updated.append((sid, kw))

    class Runners:
        def __init__(self) -> None:
            self.started: list[str] = []

        def start(self, sid: str) -> None:
            self.started.append(sid)

    jobs: list[tuple[str, str]] = []
    marks: list[tuple[str, str]] = []
    evs: list[str] = []
    sessions = Sessions()
    runners = Runners()
    state = SimpleNamespace(
        sessions=sessions,
        runners=runners,
        jobs=SimpleNamespace(
            create_job=lambda sid, kind, payload: jobs.append((sid, kind))
        ),
        autonomy=SimpleNamespace(
            mark=lambda sid, status, detail: marks.append((sid, status))
        ),
        events=SimpleNamespace(
            append=lambda sid, **kw: evs.append(kw["action"])
        ),
    )

    resumed = resume_auth_paused_sessions(state)

    assert resumed == ["a"]
    assert sessions.bumped == ["a"]
    assert sessions.updated == [("a", {"status": "queued", "error": None})]
    assert runners.started == ["a"]
    assert jobs == [("a", "research_resume")]
    assert marks == [("a", "queued")]
    assert evs == ["auto_resumed_after_signin"]


# =====================================================================
# Pipeline wiring regressions: initialization → research → report
# =====================================================================
class TestPipelineWiring:
    @pytest.mark.asyncio
    async def test_owner_pause_never_records_the_phase_as_complete(
        self, db, config, session
    ):
        """A pause INSIDE a phase must stop the run right there. Falling
        through wrote a `phase_complete` checkpoint for a half-run phase and
        could run Phase 8's integrity pipeline on a paused session."""
        sessions = SessionStore(db, config)
        cps = CheckpointStore(db)
        model = ScriptedModel(PHASE_OUTPUTS)
        runner = make_runner(db, config, model)
        original_call = model.call

        async def call_and_pause(*a, **k):
            reply = await original_call(*a, **k)
            runner.request_pause()
            return reply

        model.call = call_and_pause  # type: ignore[method-assign]
        await runner.run(session)
        assert sessions.require(session).status == "paused"
        assert cps.get(session, "paused:owner") is not None
        # Phase 1 was interrupted mid-run: it must NOT be recorded complete.
        assert cps.get(session, "phase_complete:opportunity_discovery") is None
        # Resume finishes the session and only then records completion.
        runner2 = make_runner(db, config, ScriptedModel(PHASE_OUTPUTS))
        await runner2.run(session)
        assert sessions.require(session).status == "completed"
        assert cps.get(session, "phase_complete:opportunity_discovery") is not None

    @pytest.mark.asyncio
    async def test_pause_during_phase_8_skips_the_integrity_pipeline(
        self, db, config, session
    ):
        """Pausing while adversarial verification runs must not trigger the
        deterministic integrity pipeline (or a possible focus pivot) on a
        session that is already paused — the pipeline re-runs on resume."""
        sessions = SessionStore(db, config)
        cps = CheckpointStore(db)
        model = ScriptedModel(PHASE_OUTPUTS)
        runner = make_runner(db, config, model)
        original_call = model.call

        async def call_and_pause_on_p8(*a, **k):
            reply = await original_call(*a, **k)
            if model.calls and model.calls[-1] == "adversarial_verification":
                runner.request_pause()
            return reply

        model.call = call_and_pause_on_p8  # type: ignore[method-assign]
        await runner.run(session)
        assert sessions.require(session).status == "paused"
        assert cps.get(session, "phase_complete:adversarial_verification") is None
        assert cps.get(session, "integrity_pipeline") is None
        # Resume completes, and THEN the integrity pipeline runs.
        runner2 = make_runner(db, config, ScriptedModel(PHASE_OUTPUTS))
        await runner2.run(session)
        assert sessions.require(session).status == "completed"
        assert cps.get(session, "integrity_pipeline") is not None

    @pytest.mark.asyncio
    async def test_progress_is_live_and_never_goes_backwards(
        self, db, config, session
    ):
        """The dashboard bar binds to `session.progress`: it used to sit at
        0% for a whole six-hour run and only jump to 100% at the end."""
        sessions = SessionStore(db, config)
        model = ScriptedModel(PHASE_OUTPUTS)
        seen: list[float] = []
        original_call = model.call

        async def spy(*a, **k):
            seen.append(sessions.require(session).progress)
            return await original_call(*a, **k)

        model.call = spy  # type: ignore[method-assign]
        runner = make_runner(db, config, model)
        await runner.run(session)
        final = sessions.require(session).progress
        assert final == 1.0
        assert seen and seen[0] > 0.0, "progress stayed at 0 during research"
        assert seen == sorted(seen), f"progress went backwards: {seen}"

    @pytest.mark.asyncio
    async def test_job_row_tracks_the_research_lifecycle(self, db, config, session):
        """Jobs are created by the start route and then frozen at `pending`:
        a finished session still showed a pending job. The runner now drives
        the durable job row through initializing → researching → completed."""
        sessions = SessionStore(db, config)
        events = EventLog(db)
        jobs = JobOrchestrator(db, config, sessions, events, ErrorStateStore(db))
        job = jobs.create_job(session, kind="research_run")
        assert jobs.get(session, job.id).status == "pending"

        runner = make_runner(db, config, ScriptedModel(PHASE_OUTPUTS))
        await runner.run(session)
        assert sessions.require(session).status == "completed"
        assert jobs.get(session, job.id).status == "completed"
        actions = {e.action for e in events.tail(session, limit=1000)}
        assert "job_initializing" in actions
        assert "job_researching" in actions
        assert "job_completed" in actions

    @pytest.mark.asyncio
    async def test_paused_preflight_is_not_recorded_as_done(self, db, config, session):
        """A pause during the census/frontier walk must not stamp partial
        measurements as permanently complete — the resumed session re-walks
        the parts that never finished."""
        sessions = SessionStore(db, config)
        cps = CheckpointStore(db)
        runner = make_runner(db, config, ScriptedModel(PHASE_OUTPUTS))
        runner.request_pause()  # owner pauses while the preflight walks
        await runner.run(session)
        assert sessions.require(session).status == "paused"
        cp = cps.get(session, "discovery_preflight")
        assert cp is not None
        assert cp.payload["done"] is False

        runner2 = make_runner(db, config, ScriptedModel(PHASE_OUTPUTS))
        await runner2.run(session)
        assert sessions.require(session).status == "completed"
        assert cps.get(session, "discovery_preflight").payload["done"] is True

    @pytest.mark.asyncio
    async def test_reference_images_are_prepared_and_cached(self, db, config, session):
        """Owner uploads are downscaled to bounded JPEGs once per run so the
        model can actually SEE an image-steered brief."""
        from io import BytesIO

        from PIL import Image

        from app.uploads import UploadStore

        buf = BytesIO()
        Image.new("RGBA", (3000, 2000), (200, 30, 30, 128)).save(buf, format="PNG")
        store = UploadStore(db, config)
        store.save(
            session, filename="cover.png", content_type="image/png",
            data=buf.getvalue(),
        )
        runner = make_runner(db, config, ScriptedModel(PHASE_OUTPUTS))
        images = runner._reference_images(session)
        assert len(images) == 1
        assert images[0][:2] == b"\xff\xd8"  # prepared as JPEG
        # Downscaled to the bound with aspect preserved (3000x2000 → 768x512).
        out = Image.open(BytesIO(images[0]))
        assert out.format == "JPEG" and out.size == (768, 512)
        # Cached per run — repeated prompt builds never re-encode.
        assert runner._reference_images(session) is images
        # A session without uploads attaches nothing.
        assert runner._reference_images("no-such-session") == []
