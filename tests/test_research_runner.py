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
    stable and must be referenced), and requests capture_evidence actions —
    so the min_evidence exit gates can be satisfied and the runner advances.
    """

    def __init__(self, script: dict[str, list[dict[str, Any]]]) -> None:
        self.script = script
        self.calls: list[str] = []
        self.user_prompts: dict[str, list[str]] = {}
        self.usage = type("U", (), {"calls": 0, "failed_calls": 0})()

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
        if phase != "opportunity_synthesis":
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
    registry._build_runner = lambda: make_runner(db, config, ScriptedModel(PHASE_OUTPUTS))  # type: ignore[assignment]
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
