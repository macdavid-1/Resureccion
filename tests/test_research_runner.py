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
                return self._reply_for(phase, user)
        raise AssertionError("model called with unknown phase prompt")

    async def aclose(self) -> None:
        pass


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
