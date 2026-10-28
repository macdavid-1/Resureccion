"""The research orchestrator.

Executes the deterministic methodology (app/methodology.py) phase by phase:

- model reasons inside the phase contract (prompts from app/prompts.py),
- requested browser actions are executed through the whitelisted executor
  (app/actions.py) and fresh evidence is re-fed to the model,
- exit gates (min evidence, required output fields) must pass before the
  runner advances — the model cannot skip the methodology,
- every model iteration is checkpointed, journaled, and budgeted
  (max_iterations per phase + STALL_LIMIT anti-stall pivot),
- auth walls / browser crashes / model failures pause or downgrade the run
  safely and durably; nothing is lost to a restart,
- final opportunities are validated against the opportunity contract and
  persisted only after surviving the methodology.
"""
from __future__ import annotations

import asyncio
import time
import traceback
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from app.actions import ActionError, ActionExecutor, AuthPauseRequired
from app.adjacency import gather_neighbors
from app.agent import AgentStateStore
from app.artifacts import ArtifactStore
from app.browser_manager import BrowserManager, BrowserManagerError
from app.browser_store import BrowserAuthStore, BrowserEvidenceStore, LoginWindowStore
from app.census import CategoryCensus
from app.claims import ClaimStore, EQ_INFERENCE, estimate_evidence_ids
from app.config import Config
from app.db import Database
from app.events import EventLog
from app.evidence_capture import EvidenceCollector
from app.filters import CandidateFilterEngine
from app.final_dive import FinalCandidateDive
from app.frontier import AutocompleteFrontier, seeds_for_session
from app.integrity import IntegrityAssessmentStore
from app.kdp_risk import record_keyword_screening, screen_keywords
from app.marketplace import Marketplace, resolve_plan, select_auto
from app.market_memory import MarketMemoryStore, harvest_session
from app.owner_messages import OwnerMessageStore
from app.methodology import (
    EXTERNAL_ACTIONS,
    P1_DISCOVERY,
    P3_COMPETITION,
    P4_CONSUMER,
    P5_DEMAND,
    P6_GAP,
    PHASE_ORDER,
    PHASE_TITLES,
    SESSION_PHASE_BY_METHOD_PHASE,
    SPECS,
    STALL_LIMIT,
    PhaseTiming,
    focus_pivot_directive,
    mode_profile,
    validate_opportunity,
)
from app.methodology import P8_ADVERSARIAL
from app.model_client import (
    ModelClient,
    ModelConfigError,
    ModelContractError,
    ModelError,
    ModelReply,
    ModelUsage,
    _extract_json,
)
from app.prompts import build_system_prompt, build_user_prompt
from app.recovery import CheckpointStore, ErrorStateStore
from app.redact import redact
from app.quality_gates import QualityGateRunner
from app.reports import ReportStore
from app.research_data import (
    CAND_VERIFIED,
    CandidateStore,
    EvidenceStore,
    ObservationStore,
    OpportunityStore,
    VerificationStore,
)
from app.sessions import SessionStore
from app.thresholds import get_thresholds
from app.timeutil import iso_now
from app.timing import TaskType, TimingTracker, model_timing_digest
from app.trace import (
    KIND_BROWSER,
    KIND_CANDIDATE,
    KIND_DISCOVERY,
    KIND_OWNER,
    KIND_PHASE,
    KIND_PIVOT,
    KIND_RECOVERY,
    KIND_REPORT,
    KIND_VERIFICATION,
    ActivityTrace,
    browser_text,
    candidate_created_text,
    candidate_rejected_text,
    candidate_verified_text,
    focus_pivot_text,
    marketplace_text,
    phase_text,
    pivot_text,
    recovery_text,
    verification_text,
)
from app.verification import VerificationEngine, evidence_for_candidate
from app.whitespace import WhitespaceScore, compute_whitespace, priority_lines


# Extra corrective retries per model iteration when the reply violates the
# phase's output contract (missing required top-level fields). The retry
# restates the contract explicitly; whatever JSON the model still produced
# after the retries is salvaged and flows through the normal gate machinery.
# A missing field is a degraded iteration — research NEVER pauses for it.
_CONTRACT_REPAIR_ATTEMPTS = 1

# Unexpected errors inside a run are RECOVERED this many times before the
# session is marked failed. The retry re-enters the methodology at its
# durable checkpoint cursor (completed phases reused, the interrupted
# phase resumed from its own iteration checkpoint), so a heal costs one
# extra attempt — never research quality. See ResearchRunner._self_heal.
_SELF_HEAL_ATTEMPTS = 3


@asynccontextmanager
async def _noop_keepalive():
    """Stand-in keepalive for browser objects that do not implement one.

    ResearchRunner is constructed with duck-typed browser objects in tests and
    in the live smoke scripts, so the keepalive hook is looked up rather than
    assumed. Production always passes a real BrowserManager.
    """
    yield


@dataclass
class RunnerConfig:
    """Runner-level tunables (deterministic; not model-visible)."""

    max_model_calls_per_session: int = 120      # absolute safety ceiling
    max_evidence_digest_items: int = 40         # digest size cap per prompt
    max_candidates: int = 24                    # candidates tracked at once
    max_reference_images: int = 4               # owner images attached per model call
    min_opportunities_target: int = 5           # target for auto sessions
    max_opportunities: int = 12                 # synthesis output cap
    # A strategic focus pivot spends a whole new P1→P9 pass, so it only
    # fires while this fraction of the session's hard time budget remains.
    min_remaining_session_fraction_for_pivot: float = 0.5


# Signature keys that count as REAL progress for the anti-stall guard.
# Deliberately excludes the raw evidence totals: a duplicate capture bumps
# those, and a phase re-running the same search forever would then never
# stall. Distinct sources + candidates only.
_PROGRESS_KEYS: tuple[str, ...] = ("distinct_evidence", "candidates")


# Keys that never make an analysis row substantive on their own: an item
# holding nothing but evidence ids (or an activity note) is a placeholder,
# not a finding — counting it let a phase commit a column of blank rows.
_NON_SUBSTANTIVE_KEYS = frozenset(
    {"evidence_ids", "observed_at_evidence_ids", "actions", "activity_note"}
)


def _substantive(value: Any) -> bool:
    """True when a value carries real analysis content (not blanks/ids only)."""
    if isinstance(value, str):
        return bool(value.strip())
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, dict):
        return any(
            _substantive(v)
            for k, v in value.items()
            if str(k) not in _NON_SUBSTANTIVE_KEYS
        )
    if isinstance(value, (list, tuple, set)):
        return any(_substantive(v) for v in value)
    return bool(value)


def _primary_items(output: dict[str, Any], primary: str) -> int:
    """How many SUBSTANTIVE analysis items a reply holds in its primary field.

    Only real content counts toward the exit gate: a list of eight rows whose
    verdicts are all empty strings is not eight findings, and treating it as
    such is exactly how a report ended up with a demand-verdict column of
    blanks and a consumer table full of dashes.
    """
    value = output.get(primary) if isinstance(output, dict) else None
    if not isinstance(value, list):
        return 0
    return sum(1 for item in value if _substantive(item))


class ResearchRunner:
    """One research session's lifecycle. Run via `run()` in a worker task."""

    def __init__(
        self,
        db: Database,
        config: Config,
        *,
        sessions: SessionStore,
        agent: AgentStateStore,
        events: EventLog,
        errors: ErrorStateStore,
        checkpoints: CheckpointStore,
        owner_messages: OwnerMessageStore | None = None,
        candidates: CandidateStore,
        evidence: EvidenceStore,
        observations: ObservationStore,
        opportunities: OpportunityStore,
        artifacts: ArtifactStore,
        browser: BrowserManager,
        browser_evidence: BrowserEvidenceStore,
        browser_auth: BrowserAuthStore,
        model: ModelClient,
        runner_config: RunnerConfig | None = None,
        executor: ActionExecutor | None = None,
        claims: ClaimStore | None = None,
        assessments: IntegrityAssessmentStore | None = None,
        trace: ActivityTrace | None = None,
        supervisor: Any = None,
        live_view: Any = None,
        thoughts: Any = None,
        live_session_id: str | None = None,
    ) -> None:
        self.db = db
        self.config = config
        self.sessions = sessions
        self.agent = agent
        self.events = events
        self.errors = errors
        self.checkpoints = checkpoints
        # Owner→agent mid-research messages (durable, exactly-once delivery).
        self.owner_messages = owner_messages or OwnerMessageStore(db)
        self.candidates = candidates
        self.evidence = evidence
        self.observations = observations
        self.opportunities = opportunities
        self.artifacts = artifacts
        self.browser = browser
        self.browser_evidence = browser_evidence
        self.browser_auth = browser_auth
        self.model = model
        self.rc = runner_config or RunnerConfig()
        # --- integrity layer (research integrity > everything) ---------------
        self.thresholds = get_thresholds()
        self.claims = claims or ClaimStore(db)
        self.assessments = assessments or IntegrityAssessmentStore(db)
        self.filter_engine = CandidateFilterEngine(self.thresholds, self.assessments)
        self.verification_engine = VerificationEngine(
            self.thresholds, self.claims,
            VerificationStore(db), self.assessments,
        )
        # --- autonomy layer -----------------------------------------------------
        self.trace = trace or ActivityTrace(db)
        # Owner-visible chain of thought (one row per model call; the only
        # place raw reasoning is ever written, and only for the owner).
        self.thoughts = thoughts
        self.supervisor = supervisor
        self._pause_requested = False
        self._owner_note_lines: list[str] = []
        self._owner_notes_loaded_for: str | None = None
        self._timings: dict[str, TimingTracker] = {}
        # Live elapsed tick per session (see _tick_elapsed): the sessions list
        # renders elapsed_seconds directly, so it must be folded in while the
        # run is actually running.
        self._elapsed_last: dict[str, float] = {}
        self.executor = executor or ActionExecutor(
            config,
            browser,
            EvidenceCollector(
                browser,
                browser_evidence,
                artifacts,
                # Live view: every research screenshot also refreshes the
                # bounded live-view ring so the owner can watch the browser
                # (and this session's navigation) from their phone.
                live_view=live_view,
                live_view_session_provider=(
                    (lambda: live_session_id) if live_view else None
                ),
            ),
            _RunnerAuth(config, browser, browser_auth, LoginWindowStore(db)),
            events,
        )
        self._cancelled = False
        # Per-session all-time usage (prior runs merged on resume) — see
        # _record_model_usage.
        self._usage_prior: dict[str, dict] = {}
        # Deep-discovery layers (deterministic, durable, session-scoped):
        # whitespace scores, cross-session market memory, the adjacency log.
        self._ws_scores: dict[str, dict[str, WhitespaceScore]] = {}
        self._ws_evidence_count: dict[str, int] = {}
        self.memory = MarketMemoryStore(db)
        # Owner reference images, prepared once per run (see _reference_images).
        self._ref_images: dict[str, list[bytes]] = {}

    # ------------------------------------------------- owner message channel
    def _take_owner_messages(self, session_id: str) -> None:
        """Absorb any owner messages sent while research runs.

        Non-interrupting by design: the runner never pauses or waits; it
        simply picks up whatever the owner typed whenever it next builds a
        prompt (each iteration, and once at session start). Messages are
        consumed exactly-once (delivery marker committed with the read) and
        then live permanently in `self._owner_note_lines`, which is re-sent
        in EVERY subsequent prompt — zero context loss, even across a
        crash/resume (the journal is durable in the DB).
        """
        try:
            pending = self.owner_messages.consume_pending(session_id)
        except Exception:
            return  # messaging must never break research
        for m in pending:
            line = f"(owner, {m.created_at}) {m.text}"
            if line not in self._owner_note_lines:
                self._owner_note_lines.append(line)
            self.events.append(
                session_id, level="info", actor="owner", action="owner_message_received",
                detail={"id": m.id, "text": m.text[:300]},
            )
            self.trace.record(
                session_id, KIND_OWNER,
                f"Owner message received: {m.text[:160]}",
            )

    def _owner_note_lines_for(self, session_id: str) -> list[str]:
        """Full durable owner-guidance journal for the prompt. Rehydrated
        from the DB on (re)start so a resumed session keeps every note."""
        if self._owner_notes_loaded_for != session_id:
            self._owner_notes_loaded_for = session_id
            try:
                self._owner_note_lines = [
                    f"(owner, {m.created_at}) {m.text}"
                    for m in self.owner_messages.journal(session_id)
                ]
            except Exception:
                self._owner_note_lines = []
        return list(self._owner_note_lines)

    # ------------------------------------------------------------------ cancel
    def cancel(self) -> None:
        self._cancelled = True

    def request_pause(self) -> None:
        """Owner pause: the runner checks this at safe boundaries (between
        iterations and before/after browser actions) — never mid-page."""
        self._pause_requested = True

    def _timings_for(self, session_id: str) -> TimingTracker:
        t = self._timings.get(session_id)
        if t is None:
            t = TimingTracker(self.db, session_id)
            self._timings[session_id] = t
        return t

    def _tick_elapsed(self, session_id: str) -> None:
        """Fold live running time into the durable elapsed_seconds counter.

        The sessions list reads elapsed_seconds directly, so without this
        tick the card read 0:00 for a whole run while only the detail view
        faked a timer from updated_at. The baseline is set when the run
        starts and run()'s finally flushes the final slice on every exit
        path; nothing accumulates while the session is paused.
        """
        now = time.monotonic()
        last = self._elapsed_last.get(session_id)
        self._elapsed_last[session_id] = now
        if last is not None:
            self.sessions.accumulate_elapsed(session_id, now - last)

    def _bump_progress(self, session_id: str, value: float) -> None:
        """Advance the durable, owner-visible progress estimate.

        The dashboard binds its progress bar to `session.progress`, which
        used to sit at 0% until the final `1.0` — a six-hour run looked
        frozen. Phases feed it (1/9 each, fractionally within a phase).
        Monotonic by design: a resume or a focus pivot re-runs phases, but
        the bar never jumps backwards — progress is the session's journey
        toward a completed report, not the current pass's cursor.
        Accounting only: any failure is swallowed; research never waits on it.
        """
        try:
            target = round(min(0.99, max(0.0, value)), 4)
            current = self.sessions.get(session_id)
            if current is None or target <= (current.progress or 0.0):
                return
            self.sessions.update(session_id, progress=target)
        except Exception:
            pass

    # ---------------------------------------------------------- usage/cost
    def _record_model_usage(self, session_id: str, reply: Any) -> None:
        """Checkpoint this session's cumulative token/cost usage (durable).

        `reply.usage` is the ModelClient's cumulative totals for the current
        run (the registry builds a fresh client per run); totals from PRIOR
        runs of the same session (crash/resume) are re-loaded from the
        checkpoint once and added, so the checkpoint always holds the
        session's all-time spend. This feeds the session detail endpoint so
        the owner can see what research costs. Accounting never breaks
        research — any failure here is swallowed.
        """
        u = getattr(reply, "usage", None)
        if u is None:
            return
        if session_id not in self._usage_prior:
            prior: dict = {}
            try:
                cp = self.checkpoints.get(session_id, "model_usage")
                prior = dict(cp.payload) if cp else {}
            except Exception:
                prior = {}
            self._usage_prior[session_id] = prior
        prior = self._usage_prior[session_id]
        total = {
            "calls": int(prior.get("calls", 0)) + int(getattr(u, "calls", 0)),
            "failed_calls": int(prior.get("failed_calls", 0)) + int(getattr(u, "failed_calls", 0)),
            "prompt_tokens": int(prior.get("prompt_tokens", 0)) + int(getattr(u, "prompt_tokens", 0)),
            "completion_tokens": int(getattr(u, "completion_tokens", 0)) + int(prior.get("completion_tokens", 0)),
            "cost_usd": round(float(prior.get("cost_usd", 0.0)) + float(getattr(u, "cost_usd", 0.0)), 6),
        }
        try:
            self.checkpoints.save(session_id, "model_usage", total)
        except Exception:
            pass  # usage accounting is additive; never impede research

    # ------------------------------------------------------------------- run
    async def run(self, session_id: str) -> None:
        """Execute the full methodology for a session. Never raises outward —
        every failure path ends in a durable session status.

        Self-healing wrapper: an unexpected error inside the run is
        RECOVERED (up to _SELF_HEAL_ATTEMPTS times) by re-entering the
        methodology at its durable checkpoint cursor — completed phases are
        reused and the interrupted phase resumes mid-flight, so a heal
        costs one extra attempt, never research quality. The session is
        marked failed only when the heal budget is spent (and even then it
        stays owner-resumable). Dedicated outcomes — cancel, auth/model/
        browser pause — are handled first and never loop.
        """
        session = self.sessions.require(session_id)
        session_started = time.monotonic()
        self._elapsed_last[session_id] = session_started
        heals_left = _SELF_HEAL_ATTEMPTS
        try:
            while True:
                try:
                    await self._run_once(session_id, session, session_started)
                    return
                except _Cancelled:
                    try:
                        self.sessions.update(session_id, status="cancelled", error="cancelled by owner")
                        self.agent.transition(session_id, "stopped", substate="cancelled")
                    except Exception:
                        pass  # never mask a durable cancel behind a DB hiccup
                    return
                except AuthPauseRequired as exc:
                    self._pause_for_auth(session_id, exc)
                    return
                except (ModelConfigError, ModelError) as exc:
                    self._pause_for_model(session_id, exc)
                    return
                except BrowserManagerError as exc:
                    self._pause_for_browser(session_id, exc)
                    return
                except asyncio.CancelledError:
                    self.sessions.update(session_id, status="interrupted", error="runner task cancelled")
                    self.agent.transition(session_id, "waiting_recovery", substate="task_cancelled")
                    self._mark_job(session_id, "recovering", {"note": "runner task cancelled"})
                    raise
                except Exception as exc:
                    if heals_left > 0:
                        heals_left -= 1
                        self._self_heal(session_id, exc, heals_left)
                        continue  # re-enter at the last durable checkpoint
                    self._fail(session_id, exc)
                    return
        finally:
            self._tick_elapsed(session_id)  # flush the final slice on every exit path
            self._elapsed_last.pop(session_id, None)
            await self.model.aclose()

    async def _run_once(
        self, session_id: str, session: Any, session_started: float
    ) -> None:
            """One attempt at the full methodology; run() self-heals an
            unexpected error by re-entering this from the checkpoints.

            NOTE: the body keeps its original 12-space indent from when it
            lived inside run()'s try block — consistent, so valid Python.
            """
            self.sessions.update(session_id, status="running", error=None)
            self._mark_job(session_id, "initializing", {"note": "session start"})
            # Idempotent: after a crash/resume the agent state row already
            # exists; a fresh session needs it created before transitioning.
            self.agent.initialize(session_id)
            self.agent.transition(session_id, "planning", substate="methodology_start")
            # Rehydrate any owner guidance sent before/between runs so the
            # model's very first prompt already carries it.
            self._take_owner_messages(session_id)
            self.events.append(
                session_id, level="info", actor="agent", action="research_started",
                detail={"mode": session.mode, "methodology_phases": list(PHASE_ORDER)},
            )

            # Meaningful session names: the model names the session from the
            # brief (background, best-effort, never blocks research).
            if not session.name or "Research Session" in session.name or "Market Sweep" in session.name:
                try:
                    from app.naming import generate_session_name

                    await generate_session_name(
                        session_id, sessions=self.sessions, model=self.model,
                        events=self.events,
                    )
                except Exception:
                    pass  # naming is cosmetic; research proceeds regardless

            # 0. Marketplace plan (deterministic).
            marketplaces, marketplace_mode = self._resolve_marketplaces(session)
            self.executor.set_marketplaces(marketplaces)
            self.checkpoints.save(session_id, "marketplace_plan", {
                "mode": marketplace_mode,
                "marketplaces": [m.code for m in marketplaces],
            })
            self.events.append(
                session_id, level="info", actor="agent", action="marketplace_plan_set",
                detail={"mode": marketplace_mode, "marketplaces": [m.code for m in marketplaces]},
            )
            self.trace.record(
                session_id, KIND_DISCOVERY,
                marketplace_text([m.code for m in marketplaces], marketplace_mode),
            )

            # 0.5 Deterministic discovery preflight — the census (what the
            # market actually looks like) and the autocomplete frontier (what
            # buyers actually type), gathered BEFORE the model opines so
            # Phase 1 reasons over measurements instead of its imagination.
            # Durable: a resumed session reuses it and never re-walks.
            await self._discovery_preflight(session_id, session, marketplaces)
            # Initialization is done (plan + preflight): research is now
            # doing model-visible work — the durable job row follows.
            self._mark_job(session_id, "researching", {"note": "methodology start"})

            # 1-9. The methodology. The phase order is strict; the only
            # repetition is the STRATEGIC FOCUS PIVOT: when a full pass
            # (deep research + niche-down + adversarial verification + the
            # integrity pipeline) leaves ZERO verified candidates, discovery
            # re-enters on a fresh focus, bounded by config.max_focus_pivots.
            timings = self._timings_for(session_id)
            # Durable resume cursor. The pivot count is restored from the
            # 'focus_pivots' checkpoint so the pivot budget survives
            # restarts, and every phase committed in the CURRENT pass is
            # reused from its 'phase_output' checkpoint: a resumed session
            # continues at the first incomplete phase — it never re-runs
            # the methodology from Phase 1.
            pivot_round = self._pivot_rounds(session_id)
            phase_outputs: dict[str, Any] = {}
            completed_phases: set[str] = set()
            for phase in PHASE_ORDER:
                prior_out = self._load_phase_output(session_id, phase, pivot_round)
                if prior_out is not None:
                    phase_outputs[phase] = prior_out
                    completed_phases.add(phase)
            if completed_phases:
                self.events.append(
                    session_id, level="info", actor="agent",
                    action="research_resumed_from_checkpoints",
                    detail={"completed_phases": sorted(completed_phases),
                            "pass": pivot_round},
                )
                self.trace.record(
                    session_id, KIND_PHASE,
                    f"Resuming research: reusing {len(completed_phases)} completed "
                    f"phase(s) from durable checkpoints and continuing at the first "
                    f"incomplete phase — nothing researched is lost",
                )
            while True:
                completed_all_phases = True
                for phase in PHASE_ORDER:
                    if self._cancelled:
                        raise _Cancelled()
                    if self._pause_requested:
                        self.checkpoints.save(session_id, "paused:owner", {"at": iso_now(), "phase": phase})
                        self.sessions.update(session_id, status="paused", error=None)
                        self.agent.transition(session_id, "waiting_recovery", substate="owner_pause")
                        self.events.append(session_id, level="info", actor="system", action="paused_by_owner", detail={"phase": phase})
                        self.trace.record(session_id, KIND_PHASE, f"Paused by owner before {PHASE_TITLES.get(phase, phase)}")
                        return
                    if phase in completed_phases:
                        # Completed before the interrupt: REUSED, never
                        # re-run. Exception: Phase 8's deterministic
                        # post-phase work has its own durable marker — if
                        # the interrupt hit between the phase commit and
                        # that work, run it now before skipping onward.
                        if phase == P8_ADVERSARIAL and not self._post_p8_done(session_id):
                            if self._post_adversarial(
                                session_id, session, phase_outputs,
                                pivot_round, session_started,
                            ):
                                pivot_round += 1
                                phase_outputs = {}
                                completed_phases = set()
                                completed_all_phases = False
                                break
                        else:
                            self._bump_progress(
                                session_id,
                                (PHASE_ORDER.index(phase) + 1) / len(PHASE_ORDER),
                            )
                        continue
                    # Show the phase that is RUNNING now: the row starts at
                    # 'initializing' and previously only advanced AFTER a
                    # phase finished, so the whole first phase displayed as
                    # "Initializing" on the list and detail views while the
                    # event feed already said "Entering Phase 1".
                    self.sessions.update(
                        session_id,
                        phase=SESSION_PHASE_BY_METHOD_PHASE.get(phase, "exploration"),
                    )
                    output = await self._run_phase(
                        session_id, session, phase, phase_outputs, session_started
                    )
                    phase_outputs[phase] = output
                    # Owner pause hit INSIDE the phase: stop here. Falling
                    # through would record the phase as complete, finish its
                    # timing as "ok", and (when the phase is P8) run the
                    # integrity pipeline and a possible focus pivot on a
                    # session that is already paused.
                    if output.get("_paused"):
                        return
                    # Deterministic post-phase layers. Both only ORDER and
                    # ALLOCATE: recompute headroom, then let success (not
                    # failure) trigger the next round of discovery.
                    if not output.get("_paused"):
                        self._refresh_whitespace(session_id)
                        if phase in (P3_COMPETITION, P5_DEMAND):
                            await self._adjacency_expand(
                                session_id, session, marketplaces, phase
                            )
                    self.checkpoints.save(
                        session_id, f"phase_complete:{phase}",
                        {"completed_at": iso_now(), "output_keys": sorted(output.keys()),
                         "pass": pivot_round},
                    )
                    timings.finish(f"phase:{phase}", outcome="ok")
                    if self.supervisor is not None:
                        self.supervisor.heartbeat(session_id, phase=phase, note="phase_complete")
                    self.sessions.update(
                        session_id, phase=SESSION_PHASE_BY_METHOD_PHASE.get(phase, "exploration")
                    )
                    self._bump_progress(
                        session_id,
                        (PHASE_ORDER.index(phase) + 1) / len(PHASE_ORDER),
                    )
                    # After adversarial verification (P8), the deterministic
                    # integrity pipeline filters and verifies every synthesis-bound
                    # candidate BEFORE synthesis may run.
                    if phase == P8_ADVERSARIAL:
                        # Post-phase deterministic work (integrity pipeline,
                        # market memory harvest, strategic pivot gate). It
                        # has its own durable completion marker so a resume
                        # can run it exactly once — or catch it up when the
                        # interrupt hit between phase commit and this work.
                        if self._post_adversarial(
                            session_id, session, phase_outputs,
                            pivot_round, session_started,
                        ):
                            pivot_round += 1
                            # The new pass analyzes from scratch: the dead
                            # focus's decisions must not masquerade as the
                            # new pass's. Its full record survives in the
                            # candidate ledger, events, trace, and the
                            # 'focus_pivots' checkpoint.
                            phase_outputs = {}
                            completed_phases = set()
                            completed_all_phases = False
                            break
                if completed_all_phases:
                    break

            self.agent.transition(session_id, "reporting", substate="synthesis_done")
            self.trace.record(session_id, KIND_REPORT, "Generating the final research report")
            await self._write_final_report(session_id, phase_outputs)

            self.sessions.update(session_id, status="completed", progress=1.0)
            self.agent.transition(session_id, "done")
            self._mark_job(session_id, "completed", {"note": "final report written"})
            self.events.append(
                session_id, level="info", actor="agent", action="research_completed",
                detail={"elapsed_seconds": round(time.monotonic() - session_started, 1)},
            )


    # ------------------------------------------------------------------ phases
    async def _run_phase(
        self,
        session_id: str,
        session: Any,
        phase: str,
        prior_outputs: dict[str, Any],
        session_started: float,
    ) -> dict[str, Any]:
        spec = SPECS[phase]
        # Per-mode budget profile: the SAME methodology, shaped differently
        # for a detailed brief vs keywords vs a run with no topic at all.
        max_iter = self._phase_iterations(phase, session.mode)
        phase_idx = PHASE_ORDER.index(phase)
        phase_start = time.monotonic()
        timing = PhaseTiming(phase=phase, started_at=iso_now())
        timings = self._timings_for(session_id)
        # Strategic pivot framing (durable; None on a normal pass).
        pivot_directive = self._current_pivot_directive(session_id)
        timings.start(f"phase:{phase}", TaskType.PHASE)
        self.agent.transition(session_id, spec.agent_states[0], substate=phase)
        self.events.append(
            session_id, level="info", actor="agent", action="phase_started",
            detail={"phase": phase, "max_iterations": max_iter},
        )
        self.trace.record(
            session_id, KIND_PHASE,
            phase_text(PHASE_TITLES.get(phase, phase), entering=True),
        )

        stall = 0
        model_calls = 0
        # Phase-start baseline. The min_evidence floor is measured against
        # what THIS phase captures (counts - baseline), never against the
        # session total: Phase 1 alone can hand over 100+ evidence records,
        # which used to satisfy every later floor instantly and let a phase
        # commit on its first iteration with nothing researched.
        baseline = self._progress_signature(session_id)
        last_counts = baseline
        # Last reply that filled the phase contract — applied on a forced or
        # budget exit so the model's work is never thrown away.
        best_output: dict[str, Any] | None = None
        iteration = 0
        # Durable mid-phase resume. Every completed iteration checkpoints
        # (iteration, stall, best output) for the CURRENT pass, so a paused,
        # crashed or restarted session continues this phase exactly where it
        # stopped — never from iteration 1 and never losing the analysis it
        # had already produced.
        current_pass = self._pivot_rounds(session_id)
        progress = self._load_phase_progress(session_id, phase, current_pass)
        start_iteration = int(progress.get("iteration") or 0)
        if progress:
            stall = int(progress.get("stall") or 0)
            saved_best = progress.get("best_output")
            if isinstance(saved_best, dict) and saved_best:
                best_output = saved_best
        else:
            # Bootstrap for a session paused by a build that pre-dates the
            # per-iteration checkpoint: the owner-pause record carries the
            # phase + iteration where the run stopped.
            try:
                paused = self.checkpoints.get_safe(session_id, "paused:owner")
                if paused is not None and (paused.payload or {}).get("phase") == phase:
                    start_iteration = int((paused.payload or {}).get("iteration") or 0)
            except Exception:
                pass
        iteration = start_iteration
        # Deterministic deep-discovery inputs for this phase's prompts
        # (census, frontier, market memory, headroom ordering, adjacency).
        discovery = self._discovery_digest(session_id, prior_outputs)
        # Owner reference images: prepared once per run, attached to every
        # model call so an image-only or image-steered brief stays visible
        # to the model for the WHOLE phase (a text note alone cannot steer).
        ref_images = self._reference_images(session_id)
        has_images = bool(ref_images) or self._has_images(session_id)
        for iteration in range(start_iteration + 1, max_iter + 1):
            if self._cancelled:
                raise _Cancelled()
            if self._pause_requested:
                timings.abandon(f"phase:{phase}", outcome="paused")
                self.checkpoints.save(session_id, "paused:owner", {"at": iso_now(), "phase": phase, "iteration": iteration})
                self.sessions.update(session_id, status="paused", error=None)
                self.trace.record(
                    session_id, KIND_PHASE,
                    f"Paused by owner during {PHASE_TITLES.get(phase, phase)}",
                )
                return {"_paused": True}
            # Session-wide model budget (durable, profile-aware): a resumed
            # session continues the count it already spent.
            if self._model_calls_used(session_id) >= self._model_call_ceiling(session.mode):
                self.events.append(
                    session_id, level="warn", actor="agent",
                    action="session_model_budget_reached",
                    detail={"phase": phase, "ceiling": self._model_call_ceiling(session.mode)},
                )
                break
            # Hard wall-clock ceiling per session (deterministic safety).
            elapsed_total = time.monotonic() - session_started
            if elapsed_total > self.config.session_hard_timeout_hours * 3600:
                self.events.append(
                    session_id, level="warn", actor="agent",
                    action="session_hard_timeout", detail={"elapsed_seconds": round(elapsed_total, 1)},
                )
                break
            timing.iterations = iteration
            timing.elapsed_seconds = time.monotonic() - phase_start
            timing.session_elapsed_seconds = time.monotonic() - session_started
            timing.attempts_since_progress = stall
            self._tick_elapsed(session_id)
            # Known progress: this phase's slice of the 9-phase methodology.
            self._bump_progress(
                session_id,
                (phase_idx + iteration / max(1, max_iter)) / len(PHASE_ORDER),
            )

            # --- owner message pickup (non-interrupting) -----------------
            # Checked on EVERY iteration, before the prompt is built: whatever
            # the owner typed lands in this iteration's context. Never pauses,
            # never stops, never reorders the methodology.
            self._take_owner_messages(session_id)
            # --- one model iteration -------------------------------------
            # Vision ground truth: fetch the newest browser screenshot before
            # building the prompt (the prompt notes whether one is attached).
            screenshot_png = self._latest_screenshot()
            system = build_system_prompt(phase)
            user = build_user_prompt(
                phase,
                mode=session.mode,
                objective=session.objective,
                prompt_text=session.prompt,
                marketplaces=[m.code for m in self.executor._marketplace_cycle],
                marketplace_mode=self._marketplace_mode(session_id),
                evidence_digest=self._evidence_digest(session_id),
                candidates_digest=self._candidates_digest(session_id),
                prior_phase_outputs={k: v for k, v in prior_outputs.items() if k != phase},
                timing=timing,
                images_note=(
                    (
                        f"{len(ref_images)} reference image(s) are attached to this "
                        "message — treat their subject matter as part of the brief"
                        if ref_images else
                        "reference images were uploaded; treat their subject matter "
                        "as part of the brief"
                    ) if has_images else ""
                ),
                screenshot_attached=screenshot_png is not None,
                depth_note=self._depth_note(session_started),
                owner_messages=self._owner_note_lines_for(session_id),
                pivot_directive=pivot_directive,
                census_digest=discovery.get("census") or None,
                frontier_terms=discovery.get("frontier") or None,
                market_memory=discovery.get("memory") or None,
                headroom_priority=discovery.get("priority") or None,
                adjacency_note=discovery.get("adjacency") or "",
                # Deterministic exit gate, shown so the model knows exactly
                # what still separates an explore step from a commit.
                phase_gate=self._phase_gate(
                    phase,
                    baseline=baseline,
                    latest=last_counts,
                    prior_outputs=prior_outputs,
                    session_started=session_started,
                ),
            )
            model_calls += 1
            self._bump_model_calls(session_id)
            self.agent.advance_iteration(session_id)
            self.agent.add_budget(session_id, time.monotonic() - phase_start if iteration == 1 else 0.0)
            # Per-call stuck detection: a hung model call is aborted at the
            # type-specific hard timeout and treated as a failed iteration
            # (retry budget), never a hung session.
            # One model iteration, with contract repair. A reply that
            # violates the output contract (missing required top-level
            # fields, e.g. 'niches') gets ONE corrective retry that restates
            # the contract explicitly; if it still violates, whatever JSON
            # the model did produce is salvaged and fed through the normal
            # gate machinery below (missing fields keep the phase open; the
            # stall limiter forces it with its best output). A missing field
            # is a degraded iteration — research NEVER pauses for it.
            reply: ModelReply | None = None
            call_user = user
            for contract_attempt in range(1, _CONTRACT_REPAIR_ATTEMPTS + 2):
                call_key = f"model:{phase}:{iteration}:{contract_attempt}"
                timings.start(call_key, TaskType.MODEL_CALL)
                try:
                    # Hold the browser's egress path open for the whole call.
                    # Nothing loads while the model thinks, so the connection to
                    # the current host idles; a middlebox (CONNECT/WARP bridge)
                    # can reap it mid-call and the next navigation then hangs
                    # instead of failing fast. Duck-typed for test doubles.
                    keepalive = getattr(self.browser, "keepalive_during", None) or _noop_keepalive
                    async with keepalive():
                        reply = await self.model.call(
                            session_id, system=system, user=call_user,
                            expect_fields=spec.output_contract,
                            screenshot_png=screenshot_png,
                            reference_images=ref_images,
                            # The thought's summary until the reply lands:
                            # where the model is and how far into the phase.
                            label=(
                                f"{PHASE_TITLES.get(phase, phase)} · step {iteration}"
                                + (f" · retry {contract_attempt}" if contract_attempt else "")
                            ),
                        )
                    timings.finish(call_key, outcome="ok")
                    break
                except asyncio.CancelledError:
                    timings.abandon(call_key, outcome="cancelled")
                    raise
                except ModelContractError as exc:
                    timings.finish(call_key, outcome="contract")
                    missing = [str(f) for f in getattr(exc, "missing", []) or []]
                    self.events.append(
                        session_id, level="warn", actor="model",
                        action="model_contract_violation",
                        detail={"phase": phase, "missing": missing,
                                "attempt": contract_attempt},
                    )
                    self.errors.record(
                        session_id, scope=f"model_contract:{phase}",
                        severity="warning",
                        message=f"model output missing fields: {missing}",
                        recoverable=True,
                    )
                    if contract_attempt > _CONTRACT_REPAIR_ATTEMPTS:
                        reply = self._salvage_contract_reply(session_id, exc)
                        break
                    # Corrective retry: restate the violated contract so the
                    # model sees exactly which fields it must emit this time.
                    call_user = (
                        f"{user}\n\nCRITICAL — OUTPUT CONTRACT: your previous reply "
                        f"was rejected because its JSON was missing required "
                        f"top-level field(s): {', '.join(missing) or '(unparseable)'}. "
                        f"Emit ONE JSON object containing EVERY required field of "
                        f"this phase's contract: {', '.join(spec.output_contract)}."
                    )
                    self._bump_model_calls(session_id)  # the retry is a real call
                    continue
                except BaseException:
                    timings.finish(call_key, outcome="failed")
                    raise
                finally:
                    verdict = timings.check(call_key)
                    if verdict.hard_stuck:
                        self.events.append(
                            session_id, level="warn", actor="agent",
                            action="model_call_hard_timeout", detail=verdict.to_dict(),
                        )
            if reply is None:
                # Not even salvageable JSON: a failed iteration. The stall
                # limiter forces the phase open with whatever best output it
                # has; the session keeps running — it never pauses here.
                continue
            # Durable per-run token/cost accounting (exposed on the session).
            self._record_model_usage(session_id, reply)
            if reply.elapsed_seconds and reply.elapsed_seconds >= 150:
                self.events.append(
                    session_id, level="info", actor="agent",
                    action="model_call_slow", detail={"phase": phase, "seconds": round(reply.elapsed_seconds, 1)},
                )
            # Operator-facing rationale line for this iteration (the model is
            # asked for a one-line activity_note; absent is fine).
            iter_note = str(reply.content.get("activity_note") or "").strip()
            if iter_note:
                # Tagged so the UI can lift it into the "agent now" reasoning
                # strip, apart from structural trace lines. Still an
                # operational summary — chain-of-thought is never recorded
                # in the trace: the one exception is the owner-only thought
                # row, whose SUMMARY becomes this note (a one-line summary of
                # the very call that produced it).
                self.trace.record(
                    session_id, KIND_DISCOVERY, iter_note,
                    detail={"source": "activity_note"},
                )
                if self.thoughts is not None:
                    # getattr: test doubles (and any ModelReply-shaped object)
                    # need not carry the field at all.
                    thought_id = getattr(reply, "thought_id", None)
                    if thought_id is not None:
                        self.thoughts.summarize(thought_id, iter_note)

            # --- execute requested browser actions ------------------------
            actions = reply.content.get("actions") or []
            if isinstance(actions, list) and actions:
                for a in actions[:6]:
                    if not isinstance(a, dict):
                        continue
                    if self._pause_requested:
                        timings.abandon(f"phase:{phase}", outcome="paused")
                        self.checkpoints.save(session_id, "paused:owner", {"at": iso_now(), "phase": phase})
                        self.sessions.update(session_id, status="paused", error=None)
                        self.trace.record(
                            session_id, KIND_PHASE,
                            f"Paused by owner during {PHASE_TITLES.get(phase, phase)}",
                        )
                        return {"_paused": True}
                    action_name = str(a.get("action") or "").strip()
                    if action_name not in spec.allowed_actions:
                        self.events.append(
                            session_id, level="warn", actor="agent",
                            action="action_rejected_not_allowed",
                            detail={"phase": phase, "action": action_name},
                        )
                        continue
                    # External (non-Amazon) sources are gated ON TOP of the
                    # phase whitelist: in Phase 1 they are a second wave for
                    # autonomous runs only, never the opening move.
                    if action_name in EXTERNAL_ACTIONS and not self._external_action_ok(
                        session_id, session, phase, iteration, prior_outputs
                    ):
                        self.events.append(
                            session_id, level="warn", actor="agent",
                            action="action_rejected_external_gated",
                            detail={"phase": phase, "action": action_name,
                                    "mode": session.mode, "iteration": iteration},
                        )
                        continue
                    task_key = f"action:{action_name}:{iteration}"
                    timings.start(task_key, TaskType.PAGE_NAVIGATION)
                    try:
                        await self.executor.execute(
                            session_id=session_id,
                            action=action_name,
                            args=dict(a.get("args") or {}),
                        )
                        timings.finish(task_key, outcome="ok")
                    except AuthPauseRequired:
                        timings.abandon(task_key, outcome="auth_pause")
                        raise
                    except (ActionError, BrowserManagerError) as exc:
                        timings.finish(task_key, outcome="failed")
                        self.errors.record(
                            session_id, scope=f"action:{action_name}", severity="warning",
                            message=str(exc), recoverable=True,
                        )
                        # Stuck detection: a repeatedly failing/hanging action
                        # type is skipped after its hard timeout so research
                        # continues around the broken piece.
                        v = timings.check(task_key)
                        if v.hard_stuck:
                            self.trace.record(
                                session_id, KIND_BROWSER,
                                browser_text(f"skipped {action_name} after repeated failure", ""),
                            )
                # Actions produced fresh evidence: loop again with it.

            # --- gate checks ----------------------------------------------
            missing = [f for f in spec.output_contract if f not in reply.content]
            counts = self._progress_signature(session_id)
            phase_new_evidence = max(0, counts["evidence_total"] - baseline["evidence_total"])
            primary = spec.output_contract[0]
            items = _primary_items(reply.content, primary)
            floor = self._item_floor(
                spec, self._output_floor(phase, prior_outputs, counts["candidates"])
            )
            required_evidence = self._evidence_floor(spec.min_evidence, session_started)

            # The model's most complete analysis so far survives a forced or
            # budget exit: whichever reply filled the primary contract with
            # the most items (ties → latest) becomes the phase's contribution.
            if best_output is None or items >= _primary_items(best_output, primary):
                best_output = reply.content

            # Progress = NEW sources, not new rows: a re-run of an identical
            # search writes another evidence row with the same identity, and
            # that used to reset the stall counter forever (busy, not deep).
            progressed = any(
                counts[k] != last_counts.get(k) for k in _PROGRESS_KEYS
            )
            last_counts = counts
            stall = 0 if progressed else stall + 1
            timing.attempts_since_progress = stall
            # Durable mid-phase cursor, written every iteration BEFORE the
            # gate decides: any pause, crash or restart from this point on
            # resumes the phase exactly here, with the best analysis so far.
            self._save_phase_progress(
                session_id, phase, iteration=iteration, stall=stall,
                best_output=best_output, current_pass=current_pass,
            )

            # EXPLORE vs COMMIT. A reply that requested actions is an explore
            # step: the actions run first and the phase continues with fresh
            # evidence — it can never also close the phase. Only a reply that
            # asks for no further browsing and satisfies the floors commits.
            actions_now = reply.content.get("actions")
            exploring = isinstance(actions_now, list) and len(actions_now) > 0

            # The DECLARED floors (what the phase always owes) …
            declared_floors_met = (
                not missing
                and items >= floor
                and (spec.min_evidence == 0 or phase_new_evidence >= spec.min_evidence)
            )
            # … versus the depth-raised floor, which only applies while the
            # session is still under RESEARCH_MIN_HOURS.
            depth_pending = required_evidence > spec.min_evidence
            depth_satisfied = (
                required_evidence == 0 or phase_new_evidence >= required_evidence
            )
            # Depth must never become a spin loop: when the declared research
            # is in hand but STALL_LIMIT rounds have produced no NEW distinct
            # source, committing beats re-capturing duplicates forever. The
            # ceiling is recorded durably so the report can show the session
            # stopped on its own rather than on a fabricated number.
            depth_ceiling = stall >= STALL_LIMIT and declared_floors_met
            gates_ok = (
                declared_floors_met
                and not exploring
                and depth_satisfied
            )
            if gates_ok or depth_ceiling:
                if depth_ceiling and not gates_ok:
                    self.events.append(
                        session_id, level="warn", actor="agent",
                        action="phase_committed_at_depth_ceiling",
                        detail={"phase": phase, "iterations": iteration,
                                "evidence_new": phase_new_evidence,
                                "evidence_required": required_evidence,
                                "stall": stall},
                    )
                    self.trace.record(
                        session_id, KIND_PHASE,
                        pivot_text(
                            PHASE_TITLES.get(phase, phase),
                            f"{STALL_LIMIT} rounds without a new distinct source — "
                            "committing the declared research instead of "
                            "re-capturing the same pages",
                        ),
                    )
                self.checkpoints.save(
                    session_id, f"phase_output:{phase}",
                    {"output": reply.content, "model": reply.model,
                     "iterations": iteration, "commit": "clean",
                     "evidence_new": phase_new_evidence, "at": iso_now(),
                     "pass": current_pass},
                )
                # The phase is committed: the mid-phase cursor retires.
                try:
                    self.checkpoints.delete(session_id, f"phase_progress:{phase}")
                except Exception:
                    pass
                self.events.append(
                    session_id, level="info", actor="agent", action="phase_completed",
                    detail={"phase": phase, "iterations": iteration,
                            "evidence_new": phase_new_evidence,
                            "primary_items": items,
                            "elapsed_seconds": round(time.monotonic() - phase_start, 1)},
                )
                self._apply_phase_effects(session_id, phase, reply.content)
                return reply.content

            if stall >= STALL_LIMIT:
                self.events.append(
                    session_id, level="warn", actor="agent", action="phase_stalled",
                    detail={"phase": phase, "iterations": iteration,
                            "missing_fields": missing,
                            "evidence_new": phase_new_evidence,
                            "evidence_total": counts["evidence_total"],
                            "primary_items": items,
                            "exploring": exploring},
                )
                self.trace.record(
                    session_id, KIND_PIVOT,
                    pivot_text(PHASE_TITLES.get(phase, phase), f"no new distinct evidence after {iteration} iterations; forcing the phase to contribute what it has and moving on"),
                )
                break  # the phase's best output is applied below, not dropped

        # Budget exhausted or stalled: record it AND apply whatever the model
        # actually produced. Returning {} here used to throw away every niche
        # the model had mentioned in intermediate replies.
        self.errors.record(
            session_id, scope=f"methodology:{phase}", severity="warning",
            message=f"phase {phase} ended at budget/stall without full gate pass",
            recoverable=True,
        )
        if best_output is None:
            # Durable accounting: a phase that handed over NOTHING is recorded
            # as such (the report renders it as "produced no output") instead
            # of silently looking like a completed phase with an empty list.
            self.events.append(
                session_id, level="warn", actor="agent",
                action="phase_contributed_nothing",
                detail={"phase": phase, "iterations": iteration,
                        "evidence_new": max(
                            0,
                            self._progress_signature(session_id)["evidence_total"]
                            - baseline["evidence_total"],
                        )},
            )
            return {}
        self.checkpoints.save(
            session_id, f"phase_output:{phase}",
            {"output": best_output, "iterations": iteration,
             "commit": "forced", "at": iso_now(), "pass": current_pass},
        )
        try:
            self.checkpoints.delete(session_id, f"phase_progress:{phase}")
        except Exception:
            pass
        self.events.append(
            session_id, level="warn", actor="agent", action="phase_completed_from_budget",
            detail={"phase": phase, "iterations": iteration,
                    "primary_items": _primary_items(best_output, spec.output_contract[0]),
                    "elapsed_seconds": round(time.monotonic() - phase_start, 1)},
        )
        self._apply_phase_effects(session_id, phase, best_output)
        return best_output

    def _salvage_contract_reply(
        self, session_id: str, exc: Exception
    ) -> ModelReply | None:
        """Recover the JSON a contract-violating reply DID contain.

        A partial analysis is memory: it flows through the phase gates like
        any other reply (the missing fields simply keep the phase open) and
        can become the phase's forced contribution. Only unparseable
        garbage is discarded — and even then research continues; it never
        pauses for a model output defect.
        """
        raw = str(getattr(exc, "raw", "") or "")
        try:
            content = _extract_json(raw)
        except Exception:
            content = {}
        if not isinstance(content, dict) or not content:
            self.events.append(
                session_id, level="warn", actor="model",
                action="model_contract_unsalvageable", detail={},
            )
            return None
        self.events.append(
            session_id, level="info", actor="model",
            action="model_contract_salvaged",
            detail={"fields": sorted(str(k) for k in content)[:20]},
        )
        usage = getattr(self.model, "usage", None)
        return ModelReply(
            content=content, raw=raw,
            usage=usage if isinstance(usage, ModelUsage) else ModelUsage(),
            model=str(getattr(self.model, "_model", "") or ""),
            elapsed_seconds=0.0,
        )

    # ------------------------------------------------------------ phase effects
    def _apply_phase_effects(self, session_id: str, phase: str, output: dict[str, Any]) -> None:
        """Persist the durable effects of a phase's analysis."""
        # Niching produces/updates candidate rows.
        niches = output.get("niches")
        if isinstance(niches, list):
            # The contract says niches are objects, but a model under pressure
            # emits plain strings — a string is a REAL discovered niche, and
            # silently dropping it is exactly how a session ends up with
            # "0 candidates" despite productive research. Normalize first.
            norm: list[dict[str, Any]] = []
            for n in niches:
                if isinstance(n, str) and n.strip():
                    norm.append({"niche": n.strip()[:200]})
                elif isinstance(n, dict) and str(n.get("niche") or "").strip():
                    norm.append(n)
            # Everything past the live cap is RECORDED as backlog — the tail
            # past 24 is where odd gems live, and silently dropping it was
            # throwing away discovered demand.
            overflow = [
                n for n in norm[self.rc.max_candidates:]
            ]
            if overflow:
                self._record_backlog(
                    session_id,
                    [
                        {
                            "niche": str(n.get("niche"))[:200],
                            "parent_market": str(n.get("parent_market") or ""),
                            "marketplace": str(n.get("marketplace") or ""),
                        }
                        for n in overflow
                    ],
                    reason="candidate_capacity",
                )
            for n in norm[: self.rc.max_candidates]:
                if not n.get("candidate_id"):
                    c = self.candidates.create(
                        session_id,
                        niche=str(n["niche"])[:200],
                        marketplace=str(n.get("marketplace") or "")[:20],
                        rationale=str(n.get("demand_hints") or "")[:500],
                        meta={
                            "chain": n.get("chain") or [],
                            "parent_market": n.get("parent_market") or "",
                        },
                    )
                    n["candidate_id"] = c.id
                    self.trace.record(
                        session_id, KIND_CANDIDATE,
                        candidate_created_text(
                            str(n["niche"]), str(n.get("marketplace") or ""),
                            str(n.get("demand_hints") or ""),
                        ),
                    )
        # Account for EVERY niche the analysis actually worked on. Phases 3-7
        # name niches in their own outputs (competitive maps, consumer maps,
        # demand verdicts, gap analyses, cross-market checks). When no
        # candidate row exists for one, the session researched it but the
        # ledger counted nothing — the report then read "0 candidates
        # considered" next to four researched niches, the integrity pipeline
        # had no row to judge (so zero claims, zero rejections, zero
        # verifications and every quality gate failed). Backfilling makes the
        # accounting match the work and hands the pipeline real rows, each
        # seeded with the evidence ids the analysis cited.
        self._backfill_candidates(session_id, phase, output)
        # Adversarial verdicts move candidate statuses.
        verdicts = output.get("verdicts")
        if isinstance(verdicts, list):
            for v in verdicts:
                if not isinstance(v, dict):
                    continue
                cid = str(v.get("candidate_id") or "").strip()
                niche_hint = str(v.get("niche") or "").strip()
                if cid and self.candidates.get(session_id, cid) is None:
                    # The model referenced a candidate id that does not
                    # exist — a hallucinated slug, not a real row. Before
                    # dropping the verdict, fall back to the niche text when
                    # it names ONE real candidate: a verdict about a real
                    # candidate must land even when its id was invented
                    # (dropping it silently left real candidates stuck in
                    # "discovered" and the dashboard count at zero).
                    match = (
                        self._find_candidate_by_niche(session_id, niche_hint)
                        if niche_hint else None
                    )
                    if match is None:
                        self.events.append(
                            session_id, level="warn", actor="agent",
                            action="verdict_skipped_unknown_candidate",
                            detail={
                                "candidate_id": cid[:80],
                                "niche": niche_hint[:120],
                                "verdict": str(v.get("verdict") or "")[:40],
                                "phase": phase,
                            },
                        )
                        continue
                    self.events.append(
                        session_id, level="warn", actor="agent",
                        action="verdict_id_corrected_by_niche",
                        detail={"candidate_id": cid[:80], "matched": match.id,
                                "niche": niche_hint[:120], "phase": phase},
                    )
                    cid = match.id
                if not cid:
                    match = (
                        self._find_candidate_by_niche(session_id, niche_hint)
                        if niche_hint else None
                    )
                    if match is None:
                        self.events.append(
                            session_id, level="warn", actor="agent",
                            action="verdict_skipped_unknown_candidate",
                            detail={
                                "candidate_id": "",
                                "niche": niche_hint[:120],
                                "verdict": str(v.get("verdict") or "")[:40],
                                "phase": phase,
                            },
                        )
                        continue
                    cid = match.id
                verdict = str(v.get("verdict") or "").lower().strip()
                if verdict in ("pass", "verified"):
                    self.candidates.set_status(session_id, str(cid), "verified")
                elif verdict in ("reject", "rejected"):
                    self.candidates.set_status(session_id, str(cid), "rejected")
                elif verdict == "downgrade":
                    # A downgrade is a serious finding, not a death sentence:
                    # the candidate returns to the pool for re-niching rather
                    # than being silently destroyed.
                    self.candidates.set_status(session_id, str(cid), "discovered")
                    self.candidates.update_score(session_id, str(cid), -1.0)
        # Opportunities: only synthesis output creates them, fully validated.
        # Chain-of-custody: a synthesis-stage opportunity must map onto an
        # EXISTING verified candidate — the integrity pipeline's verdict is
        # the gate. Orphan "opportunities" with no verified candidate are
        # rejected with a durable reason; they never silently materialize.
        opps = output.get("opportunities")
        if isinstance(opps, list):
            created = 0
            for o in opps:
                if created >= self.rc.max_opportunities:
                    break
                if not isinstance(o, dict):
                    continue
                ok, problems = validate_opportunity(o)
                if not ok:
                    self.events.append(
                        session_id, level="warn", actor="agent",
                        action="opportunity_rejected_invalid",
                        detail={"problems": problems, "niche": str(o.get("niche", ""))[:120]},
                    )
                    continue
                cid = str(o.get("candidate_id") or "").strip()
                if not cid:
                    match = self._find_candidate_by_niche(session_id, str(o.get("niche", "")))
                    cid = match.id if match else ""
                cand = self.candidates.get(session_id, cid) if cid else None
                if cand is None or cand.status != "verified":
                    self.events.append(
                        session_id, level="warn", actor="agent",
                        action="opportunity_rejected_not_verified",
                        detail={
                            "niche": str(o.get("niche", ""))[:120],
                            "candidate_status": cand.status if cand else "missing",
                            "reason": (
                                "synthesis output has no matching candidate; the integrity "
                                "pipeline never verified it"
                                if cand is None else
                                f"candidate did not pass deterministic verification "
                                f"(status {cand.status!r})"
                            ),
                        },
                    )
                    self.trace.record(
                        session_id, KIND_VERIFICATION,
                        candidate_rejected_text(
                            str(o.get("niche", ""))[:120],
                            ["the integrity pipeline did not verify this candidate"],
                        ),
                    )
                    continue
                keywords = o.get("keywords") or []
                title_concepts = o.get("title_concepts")
                title = (
                    str(title_concepts[0])
                    if isinstance(title_concepts, list) and title_concepts
                    else str(o.get("niche", "Opportunity"))
                )
                # Metadata/keyword integrity: keywords that are trademark-
                # risky, deceptive, or unrelated are stripped BEFORE the
                # opportunity is persisted — discoverability must never come
                # from manipulation. The screen is recorded durably so the
                # owner sees exactly what was dropped and why.
                kw_screen = screen_keywords([str(k) for k in keywords], niche=o.get("niche", ""))
                record_keyword_screening(session_id, cid, kw_screen, self.assessments)
                blocked_kws = {
                    str(flag).split(":", 1)[1]
                    for flag in kw_screen.blocking
                    if str(flag).startswith(("keyword_trademark:", "keyword_deceptive:"))
                }
                clean_keywords = [k for k in (str(kw) for kw in keywords) if k.lower() not in blocked_kws]
                if blocked_kws:
                    self.events.append(
                        session_id, level="warn", actor="agent",
                        action="keywords_removed_integrity",
                        detail={"candidate_id": cid, "removed": sorted(blocked_kws)},
                    )
                # The authoritative verification status comes from the
                # deterministic engine, never from the model's claim.
                verification_status = self._authoritative_verification_status(session_id, cid)
                # Chain-of-custody applies to the dossier too: evidence ids
                # on the opportunity itself are filtered to ids that EXIST in
                # this session — fabricated ids never reach the report.
                meta = {k: o.get(k) for k in (
                    "parent_market", "target_reader", "reader_problem",
                    "competitive_landscape", "consumer_needs", "market_gap",
                    "differentiation", "risks", "title_concepts",
                ) if o.get(k) is not None}
                meta["evidence_ids"] = self._valid_evidence_ids(
                    session_id, [str(e) for e in (o.get("evidence_ids") or [])]
                )
                meta["verification_status"] = verification_status
                meta["keyword_integrity"] = kw_screen.to_dict()
                self.opportunities.create(
                    session_id,
                    candidate_id=cid,
                    title=title[:200],
                    niche=str(o.get("niche", ""))[:200],
                    marketplace=",".join(map(str, o.get("marketplaces") or []))[:60],
                    angle=str(o.get("positioning") or "")[:500],
                    keywords=[k[:80] for k in clean_keywords][:20],
                    confidence=float(o.get("confidence") or 0.0),
                    meta=meta,
                )
                created += 1
            if created:
                self.events.append(
                    session_id, level="info", actor="agent", action="opportunities_created",
                    detail={"count": created},
                )

    # ------------------------------------------------ deep-discovery layers
    def _phase_iterations(self, phase: str, mode: str) -> int:
        """Effective iteration cap for this phase under the mode's profile."""
        spec = SPECS[phase]
        try:
            return mode_profile(mode).iterations_for(phase, spec.max_iterations)
        except Exception:
            return spec.max_iterations

    def _model_calls_used(self, session_id: str) -> int:
        """Durable, session-wide model-call spend (survives restarts)."""
        try:
            cp = self.checkpoints.get_safe(session_id, "model_budget")
            return int((cp.payload or {}).get("used", 0)) if cp else 0
        except Exception:
            return 0

    def _bump_model_calls(self, session_id: str) -> None:
        try:
            used = self._model_calls_used(session_id) + 1
            self.checkpoints.save(
                session_id, "model_budget", {"used": used, "at": iso_now()}
            )
        except Exception:
            pass  # budget accounting must never break the call itself

    def _model_call_ceiling(self, mode: str) -> int:
        """Per-session model-call ceiling, derived from the TIME budget.

        A fixed cap made the call budget the thing that ended research: on an
        8–12 hour wall-clock budget, runs died after ~120 calls — an hour or
        two in — with most of the time unspent. The ceiling now scales with
        the hard budget (MODEL_CALLS_PER_HOUR × hours, never below the
        runner's floor) plus the mode's profile extra, so TIME decides when
        research is finished, not a call counter. A hard timeout still caps
        the run, and per-phase iteration caps still bound each phase."""
        base = self.rc.max_model_calls_per_session
        try:
            env = int(getattr(self.config, "mode_model_calls", {}).get(mode, 0) or 0)
        except (TypeError, ValueError):
            env = 0
        if env:
            return max(base, env)
        profile = mode_profile(mode)
        hard = max(1, int(getattr(self.config, "session_hard_timeout_hours", 12)))
        per_hour = max(1, int(getattr(self.config, "model_calls_per_hour", 30) or 30))
        extra = int(round(profile.extra_calls * min(1.0, hard / 12.0)))
        return max(base, hard * per_hour) + extra

    def _evidence_floor(self, min_evidence: int, session_started: float) -> int:
        """New-evidence floor for a phase, depth-raised while the session is
        still under its minimum research duration.

        Floors a single capture batch satisfies let a phase close on its
        first pass; raising them while hours remain is what converts spare
        time into deeper research instead of an early finish. Once the
        minimum is met (or RESEARCH_MIN_HOURS=0), the declared floor stands.
        """
        if min_evidence <= 0:
            return 0
        try:
            min_seconds = float(getattr(self.config, "research_min_hours", 0) or 0) * 3600
        except (TypeError, ValueError):
            min_seconds = 0.0
        if min_seconds <= 0 or (time.monotonic() - session_started) >= min_seconds:
            return min_evidence
        mult = max(1, int(getattr(self.config, "depth_evidence_multiplier", 1) or 1))
        return int(min_evidence) * mult

    # ---------------------------------- census + autocomplete frontier
    async def _discovery_preflight(
        self, session_id: str, session: Any, marketplaces: list[Marketplace]
    ) -> None:
        """Deterministic preflight: walk the market (census), then mine what
        buyers type (autocomplete frontier). Runs BEFORE Phase 1 so discovery
        reasons over measurements, and runs ONCE per session — the durable
        checkpoint makes a resumed session reuse it instead of re-walking.
        """
        try:
            cp = self.checkpoints.get_safe(session_id, "discovery_preflight")
            payload = dict(cp.payload) if cp else {}
        except Exception:
            payload = {}
        if payload.get("done"):
            return
        cfg = self.config
        census_out: dict[str, Any] = {}
        frontier_out: dict[str, Any] = {}
        # A pause or cancel stops the walk via `should_stop` — a PARTIALLY
        # measured map must never be recorded as `done`, or a resumed session
        # would treat it as the full map and never re-walk. Each half that
        # finished cleanly is cached on its own checkpoint, so an interrupt
        # during the frontier still reuses the finished census (and vice
        # versa) instead of paying for the whole walk twice.
        try:
            ccp = self.checkpoints.get_safe(session_id, "discovery_preflight_census")
            if ccp is not None and not (ccp.payload or {}).get("error"):
                census_out = dict(ccp.payload or {})
        except Exception:
            census_out = {}
        try:
            fcp = self.checkpoints.get_safe(session_id, "discovery_preflight_frontier")
            if fcp is not None and not (fcp.payload or {}).get("error"):
                frontier_out = dict(fcp.payload or {})
        except Exception:
            frontier_out = {}

        interrupted = self._cancelled or self._pause_requested
        will_census = bool(cfg.census_enabled) and not census_out and not interrupted
        will_frontier = bool(cfg.frontier_enabled) and not frontier_out and not interrupted
        if will_census or will_frontier:
            # Owner visibility: this can take a few minutes on a 2-core box,
            # so it must never look like a hung session.
            self.agent.transition(session_id, "exploring", substate="discovery_preflight")
            self.events.append(
                session_id, level="info", actor="system",
                action="discovery_preflight_started",
                detail={"census": bool(cfg.census_enabled), "frontier": bool(cfg.frontier_enabled)},
            )
            self.trace.record(
                session_id, KIND_DISCOVERY,
                "Gathering the measured map first: category census + autocomplete "
                "demand-language frontier (deterministic backend work, no model)",
            )

        if will_census:
            try:
                census = CategoryCensus(
                    self.browser,
                    self.executor.collector.evidence_store,
                    self.events,
                    max_categories=cfg.census_max_categories,
                    items_per_category=cfg.census_items_per_category,
                    max_marketplaces=cfg.census_max_marketplaces,
                    time_budget_seconds=cfg.census_time_budget_seconds,
                    should_stop=lambda: self._cancelled or self._pause_requested,
                )
                res = await census.run(
                    session_id=session_id, marketplaces=marketplaces
                )
                census_out = res.to_dict()
                self.trace.record(
                    session_id, KIND_DISCOVERY,
                    f"Deterministic market census: {res.charts_captured} chart(s) and "
                    f"{res.visited_nodes} browse node(s) measured — discovery now has "
                    "a measured map instead of the model's imagination",
                )
                if not (self._cancelled or self._pause_requested) and "error" not in census_out:
                    self.checkpoints.save(
                        session_id, "discovery_preflight_census", census_out
                    )
            except Exception as exc:
                census_out = {"error": str(exc)[:200]}
                self.errors.record(
                    session_id, scope="census", severity="warning",
                    message=str(exc)[:300], recoverable=True,
                )

        if will_frontier:
            try:
                seeds = seeds_for_session(
                    session.prompt or "",
                    session.objective or "",
                    census_categories=census_out.get("category_names") or [],
                )
                frontier = AutocompleteFrontier(
                    self.browser,
                    self.executor.collector.evidence_store,
                    self.events,
                    max_queries=cfg.frontier_max_queries,
                    max_depth=cfg.frontier_max_depth,
                    time_budget_seconds=cfg.frontier_time_budget_seconds,
                    should_stop=lambda: self._cancelled or self._pause_requested,
                )
                fres = await frontier.run(
                    session_id=session_id,
                    marketplaces=marketplaces,
                    seeds=seeds,
                )
                frontier_out = fres.to_dict()
                self.trace.record(
                    session_id, KIND_DISCOVERY,
                    f"Autocomplete frontier: {len(fres.terms)} demand-language "
                    f"phrase(s) mined from {fres.queries_run} suggestion query(ies) "
                    f"({fres.stopped_reason})",
                )
                if not (self._cancelled or self._pause_requested) and "error" not in frontier_out:
                    self.checkpoints.save(
                        session_id, "discovery_preflight_frontier", frontier_out
                    )
            except Exception as exc:
                frontier_out = {"error": str(exc)[:200]}
                self.errors.record(
                    session_id, scope="frontier", severity="warning",
                    message=str(exc)[:300], recoverable=True,
                )

        # Durable, atomic: on resume the preflight is REUSED, never repeated.
        # `done` is set only when the walk was NOT interrupted — a paused or
        # cancelled run leaves it False so the resumed session re-walks the
        # parts that never finished.
        stopped = self._cancelled or self._pause_requested
        self.checkpoints.save(
            session_id,
            "discovery_preflight",
            {
                "done": not stopped,
                "census": census_out,
                "frontier": frontier_out,
                "at": iso_now(),
            },
        )

    def _census_categories(self, session_id: str) -> list[dict[str, Any]]:
        try:
            cp = self.checkpoints.get_safe(session_id, "discovery_preflight")
            census = (cp.payload or {}).get("census") or {} if cp else {}
        except Exception:
            return []
        return [
            c for c in (census.get("categories") or []) if isinstance(c, dict)
        ][:30]

    def _frontier_terms(self, session_id: str) -> list[str]:
        try:
            cp = self.checkpoints.get_safe(session_id, "discovery_preflight")
            frontier = (cp.payload or {}).get("frontier") or {} if cp else {}
        except Exception:
            return []
        return [
            str(t) for t in (frontier.get("terms") or [])
            if isinstance(t, str) and t.strip()
        ][:60]

    def _discovery_digest(self, session_id: str, prior_outputs: dict[str, Any]) -> dict[str, Any]:
        """Everything deterministic the model should see this phase: the
        census map, the demand language, cross-session memory, the headroom
        ordering and the latest adjacency round. Empty sections are omitted
        so a lean run's prompts stay lean."""
        out: dict[str, Any] = {}
        census = self._census_categories(session_id)
        if census:
            out["census"] = census
        terms = self._frontier_terms(session_id)
        if terms:
            out["frontier"] = terms
        memory = self._memory_digest(session_id)
        if memory:
            out["memory"] = memory
        try:
            scores = self._whitespace_scores(session_id)
            cands = self.candidates.list(session_id, limit=self.rc.max_candidates)
            lines = priority_lines(cands, scores, limit=8)
            if lines:
                out["priority"] = lines
        except Exception:
            pass
        note = self._adjacency_note(session_id)
        if note:
            out["adjacency"] = note
        return out

    def _memory_digest(self, session_id: str) -> list[dict[str, Any]]:
        if not getattr(self.config, "memory_enabled", True):
            return []
        try:
            return self.memory.digest(
                fresh_days=self.config.memory_fresh_days,
                limit=self.config.memory_max_digest,
                exclude_session=session_id,
            )
        except Exception:
            return []  # memory is an accelerator, never a dependency

    def _harvest_memory(self, session_id: str) -> None:
        """Distill this session's measured facts into cross-session memory."""
        if not getattr(self.config, "memory_enabled", True):
            return
        try:
            scores = self._whitespace_scores(session_id)
            written = harvest_session(
                self.memory,
                session_id,
                candidates=self.candidates,
                browser_evidence=self.browser_evidence,
                whitespace_by_candidate={
                    k: v.to_dict() for k, v in scores.items()
                },
            )
            self.events.append(
                session_id, level="info", actor="system",
                action="market_memory_harvested", detail=written,
            )
        except Exception as exc:
            self.errors.record(
                session_id, scope="market_memory", severity="warning",
                message=str(exc)[:300], recoverable=True,
            )

    def _adjacency_rows(self, session_id: str) -> list[dict[str, Any]]:
        """Flat neighbour rows for the report's adjacency appendix."""
        try:
            cp = self.checkpoints.get_safe(session_id, "adjacency_log")
            rounds = list((cp.payload or {}).get("rounds") or []) if cp else []
        except Exception:
            return []
        rows: list[dict[str, Any]] = []
        for r in rounds:
            if not isinstance(r, dict):
                continue
            for n in r.get("neighbors") or []:
                if isinstance(n, dict):
                    rows.append({**n, "phase": r.get("phase")})
        return rows[:60]

    def _adjacency_note(self, session_id: str) -> str:
        try:
            cp = self.checkpoints.get_safe(session_id, "adjacency_log")
            rounds = list((cp.payload or {}).get("rounds") or []) if cp else []
        except Exception:
            return ""
        if not rounds:
            return ""
        last = rounds[-1]
        promoted = [
            str(n.get("term")) for n in (last.get("neighbors") or [])
            if isinstance(n, dict) and n.get("promoted")
        ]
        measured = int(last.get("measured") or 0)
        lines = [
            f"{last.get('promoted', 0)} adjacent candidate(s) were promoted "
            f"mid-run after {measured} measured search(es): "
            + "; ".join(p[:60] for p in promoted[:6])
            if promoted else
            f"{measured} adjacent neighbour(s) were measured this round; none "
            "cleared the headroom bar for promotion."
        ]
        lines.append(
            "These are markets BESIDE a winner: research them alongside the "
            "existing candidates (they carry seed evidence ids)."
        )
        return " ".join(lines)

    # -------------------------------------------------- whitespace (headroom)
    def _whitespace_scores(self, session_id: str) -> dict[str, WhitespaceScore]:
        """Recompute and persist every candidate's headroom score.

        Deterministic, evidence-only: scores are stored on the candidate's
        meta so the dashboard, report and next run all read the same number.
        The score never enters any verdict — it orders work, nothing else.
        """
        rows = self._all_browser_evidence(session_id)
        cand_rows = self.candidates.list(session_id, limit=500)
        scores: dict[str, WhitespaceScore] = {}
        for cand in cand_rows:
            try:
                claims = self.claims.list(
                    session_id, candidate_id=cand.id, limit=200
                )
                ev = evidence_for_candidate(cand, rows, claims=claims)
                score = compute_whitespace(cand, ev)
                scores[cand.id] = score
                prior = (cand.meta or {}).get("whitespace") or {}
                if prior.get("score") != score.to_dict()["score"]:
                    self.candidates.update_meta(
                        session_id, cand.id, {"whitespace": score.to_dict()}
                    )
            except Exception:
                continue  # one odd candidate never blocks the rest
        self._ws_scores[session_id] = scores
        return scores

    def _refresh_whitespace(self, session_id: str) -> dict[str, WhitespaceScore]:
        return self._whitespace_scores(session_id)

    # ---------------------------------------------------- adjacency expansion
    async def _adjacency_expand(
        self,
        session_id: str,
        session: Any,
        marketplaces: list[Marketplace],
        phase: str,
    ) -> None:
        """Walk outward from every strong candidate after competition/demand
        work: measure neighbouring demand language with REAL searches, then
        promote only what clears the headroom bar. Failures are recorded and
        researched around; adjacency can slow a run down, never break it."""
        cfg = self.config
        if not getattr(cfg, "adjacency_enabled", True):
            return
        if self._cancelled or self._pause_requested:
            return
        log_entry: dict[str, Any] = {
            "phase": phase, "at": iso_now(), "measured": 0, "promoted": 0,
            "neighbors": [],
        }
        try:
            rows = self._all_browser_evidence(session_id)
            cands = self.candidates.list(session_id, limit=500)
            known = [c.niche for c in cands]
            count = len(cands)
            neighbors = gather_neighbors(
                candidates=[c for c in cands if c.status != "rejected"],
                evidence_records=rows,
                frontier_terms=self._frontier_terms(session_id),
                exclude_niches=known,
                limit=24,
            )
            if not neighbors:
                return
            capacity = self.rc.max_candidates
            budget = min(len(neighbors), max(4, int(cfg.adjacency_max_new) * 2))
            for nb in neighbors:
                if log_entry["promoted"] >= cfg.adjacency_max_new:
                    break
                if log_entry["measured"] >= budget:
                    break
                if self._cancelled or self._pause_requested:
                    break
                if count >= capacity:
                    self._record_backlog(
                        session_id,
                        [{"niche": nb.term, "parent": nb.parent_niche,
                          "source": nb.source}],
                        reason="candidate_capacity",
                    )
                    continue
                log_entry["measured"] += 1
                try:
                    await self.executor.execute(
                        session_id=session_id,
                        action="search_marketplace",
                        args={
                            "query": nb.term,
                            "marketplace": nb.marketplace or "",
                            "screenshot": False,
                            "max_items": 12,
                        },
                    )
                except (ActionError, BrowserManagerError) as exc:
                    self.errors.record(
                        session_id, scope=f"adjacency:{nb.term[:40]}",
                        severity="warning", message=str(exc)[:200],
                        recoverable=True,
                    )
                    continue
                now_rows = self._all_browser_evidence(session_id)
                mine = [
                    r for r in now_rows
                    if str((r.get("data") or {}).get("query") or "").strip().lower()
                    == nb.term.lower()
                ] + [
                    r for r in now_rows if r.get("id") in (nb.evidence_ids or [])
                ]
                score = compute_whitespace(_NicheProbe(nb.term), mine)
                neighbor_row = {**nb.to_dict(), "whitespace": score.to_dict(),
                                 "promoted": False}
                if score.measured and (score.score or 0.0) >= float(
                    cfg.adjacency_min_whitespace
                ):
                    meta = redact({
                        "via": "adjacency",
                        "parent_candidate": nb.parent_candidate_id,
                        "parent_market": nb.parent_niche,
                        "source": nb.source,
                        "whitespace": score.to_dict(),
                        "seed_evidence_ids": [
                            r.get("id") for r in mine if r.get("id")
                        ][:8],
                        "chain": [nb.parent_niche, nb.term],
                    })
                    self.candidates.create(
                        session_id,
                        niche=nb.term[:200],
                        marketplace=nb.marketplace or "",
                        rationale=(
                            f"adjacent to '{nb.parent_niche[:70]}' ({nb.source}) — "
                            f"{score.note()}"
                        ),
                        meta=meta,
                    )
                    count += 1
                    log_entry["promoted"] += 1
                    neighbor_row["promoted"] = True
                    self.trace.record(
                        session_id, KIND_CANDIDATE,
                        candidate_created_text(nb.term, nb.marketplace or "", score.note()),
                    )
                    self.events.append(
                        session_id, level="info", actor="agent",
                        action="adjacency_candidate_promoted",
                        detail={"niche": nb.term[:120], "parent": nb.parent_niche[:120],
                                "source": nb.source, "whitespace": score.to_dict()},
                    )
                log_entry["neighbors"].append(neighbor_row)
        except Exception as exc:
            self.errors.record(
                session_id, scope="adjacency", severity="warning",
                message=f"{type(exc).__name__}: {exc}"[:300], recoverable=True,
            )
            return
        # Durable log: prompts and the report read it back.
        try:
            prior = self.checkpoints.get_safe(session_id, "adjacency_log")
            rounds = list((prior.payload or {}).get("rounds") or []) if prior else []
            rounds.append(log_entry)
            self.checkpoints.save(session_id, "adjacency_log", {"rounds": rounds[-12:]})
            if log_entry["measured"]:
                self.events.append(
                    session_id, level="info", actor="agent",
                    action="adjacency_round_completed",
                    detail={"phase": phase, "measured": log_entry["measured"],
                            "promoted": log_entry["promoted"]},
                )
        except Exception:
            pass

    # ------------------------------------------------------------- backlog
    def _record_backlog(
        self, session_id: str, items: list[dict[str, Any]], *, reason: str
    ) -> None:
        """Candidates beyond the live cap are RECORDED, never silently lost —
        the tail past 24 is exactly where odd gems live."""
        if not items:
            return
        try:
            prior = self.checkpoints.get_safe(session_id, "candidate_backlog")
            rows = list((prior.payload or {}).get("items") or []) if prior else []
            for it in items:
                row = {k: v for k, v in it.items() if v not in (None, "")}
                if not str(row.get("niche") or "").strip():
                    continue
                row["reason"] = reason
                row["at"] = iso_now()
                rows.append(row)
            seen: set[str] = set()
            deduped: list[dict[str, Any]] = []
            for row in rows:
                key = str(row.get("niche") or "").strip().lower()
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(row)
            self.checkpoints.save(
                session_id, "candidate_backlog",
                {"items": deduped[:200], "count": len(deduped)},
            )
        except Exception:
            pass  # backlog bookkeeping must never break phase handling

    def _backlog_items(self, session_id: str) -> list[dict[str, Any]]:
        try:
            cp = self.checkpoints.get_safe(session_id, "candidate_backlog")
            return [
                r for r in ((cp.payload or {}).get("items") or [])
                if isinstance(r, dict)
            ] if cp else []
        except Exception:
            return []

    # -------------------------------------------- external-source gate
    def _external_action_ok(
        self,
        session_id: str,
        session: Any,
        phase: str,
        iteration: int,
        prior_outputs: dict[str, Any],
    ) -> bool:
        """Where outside-Amazon research may fire.

        P4/P5/P6 (consumer, demand, gap): always — Amazon-side candidates
        already exist there, so external signals corroborate or kill rather
        than mislead. P1: autonomous runs ONLY, never on the opening move,
        and only once Amazon-side anchors exist — unanchored forum noise may
        not set the direction of a six-hour run.
        """
        if phase in (P4_CONSUMER, P5_DEMAND, P6_GAP):
            return True
        if phase != P1_DISCOVERY:
            return False
        if session.mode != "auto":
            return False
        if iteration < 2:
            return False
        try:
            rows = self._all_browser_evidence(session_id)
        except Exception:
            return False
        anchor_kinds = {
            "search_results", "census", "autocomplete", "autocomplete_frontier",
            "page_state", "product_page", "demand_language",
        }
        return any(r.get("kind") in anchor_kinds for r in rows)

    # ------------------------------------------------------- integrity layer
    # Niches analysed by phases 3-7 that must exist as candidate rows:
    # (output field, field holding the analysis text used as the rationale)
    _ANALYSIS_FIELDS: tuple[tuple[str, str], ...] = (
        ("competitive_maps", "saturation"),
        ("consumer_maps", "praises"),
        ("demand_assessments", "verdict"),
        ("gap_analyses", "competitors_fail"),
        ("cross_market_checks", "notes"),
    )

    def _backfill_candidates(
        self, session_id: str, phase: str, output: dict[str, Any]
    ) -> None:
        """Create candidate rows for niches the analysis researched but the
        niche phase never registered.

        Deterministic and additive: a niche already on the ledger is never
        touched, and everything past the live capacity cap is recorded as
        backlog (never silently dropped), exactly like Phase 2's output.
        """
        if not isinstance(output, dict):
            return
        known = {
            str(c.niche).strip().lower()
            for c in self.candidates.list(session_id, limit=500)
        }

        def _already_covered(key: str) -> bool:
            """Same niche under a slightly different label.

            Analysis phases phrase a niche shorter than niching did ("sudden-
            loss grief journals" vs "…for adult children"); registering it
            again would double-count the candidate and make the ledger read
            four candidates where three were researched. Only substantial
            labels participate in the containment test, so a short generic
            word can never swallow a distinct niche.
            """
            if key in known:
                return True
            if len(key) < 8:
                return False
            return any(key in k or (len(k) >= 8 and k in key) for k in known)

        fresh: list[dict[str, Any]] = []
        for field, text_key in self._ANALYSIS_FIELDS:
            rows = output.get(field)
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                niche = str(row.get("niche") or "").strip()
                key = niche.lower()
                # A niche is a market description, not a word: anything under
                # 8 characters is a fragment and never becomes a ledger row.
                if not niche or len(niche) < 8 or _already_covered(key):
                    continue
                known.add(key)
                evidence_ids = [
                    str(e) for e in (row.get("evidence_ids") or []) if str(e)
                ][:12]
                fresh.append({
                    "niche": niche[:200],
                    "marketplace": str(row.get("marketplace") or "")[:20],
                    "rationale": str(row.get(text_key) or row.get("niche") or "")[:500],
                    "evidence_ids": self._valid_evidence_ids(session_id, evidence_ids),
                    "field": field,
                })
        if not fresh:
            return
        overflow = fresh[self.rc.max_candidates:]
        if overflow:
            self._record_backlog(
                session_id,
                [
                    {
                        "niche": f["niche"],
                        "parent_market": "",
                        "marketplace": f["marketplace"],
                    }
                    for f in overflow
                ],
                reason="candidate_capacity",
            )
        for f in fresh[: self.rc.max_candidates]:
            meta: dict[str, Any] = {
                "via": phase,
                "analysis_field": f["field"],
            }
            if f["evidence_ids"]:
                meta["seed_evidence_ids"] = f["evidence_ids"]
            c = self.candidates.create(
                session_id,
                niche=f["niche"],
                marketplace=f["marketplace"],
                rationale=f["rationale"],
                meta=meta,
            )
            self.events.append(
                session_id, level="info", actor="agent",
                action="candidate_backfilled_from_analysis",
                detail={"candidate_id": c.id, "niche": f["niche"][:120],
                        "field": f["field"], "phase": phase},
            )
            self.trace.record(
                session_id, KIND_CANDIDATE,
                candidate_created_text(
                    f["niche"], f["marketplace"], f["rationale"]
                ),
            )

    def _run_integrity_pipeline(self, session_id: str, phase_outputs: dict[str, Any]) -> None:
        """Deterministic integrity pipeline, run after Phase 8 and before Phase 9.

        For every non-rejected candidate:
          1. register the candidate's claims from the phase analyses,
          2. run the 13 named deterministic filters,
          3. run the verification engine (evidence chain, claim integrity,
             corroboration, KDP risk).

        Only candidates the pipeline accepts or downgrades may feed synthesis;
        rejects carry durable, auditable reasons. This is the system's own
        check — it runs regardless of what the model asserted in Phase 8.
        """
        self.events.append(
            session_id, level="info", actor="agent", action="integrity_pipeline_started", detail={},
        )
        all_evidence = self._all_browser_evidence(session_id)
        evidence_kinds = {
            str(e.get("id") or ""): str(e.get("kind") or "")
            for e in all_evidence if e.get("id")
        }
        # Extension-derived records are VENDOR ESTIMATES: they are excluded
        # from coverage/corroboration and cannot support a claim on their
        # own until deeper research corroborates them (app.claims).
        estimate_ids = estimate_evidence_ids(all_evidence)
        candidates = [
            c for c in self.candidates.list(session_id, limit=self.rc.max_candidates)
            if c.status != "rejected"
        ]
        existing_niches = [c.niche for c in self.candidates.list(session_id, limit=500)]
        accepted = 0
        downgraded = 0
        rejected = 0
        for cand in candidates:
            self._register_candidate_claims(
                session_id, cand, phase_outputs, evidence_kinds=evidence_kinds
            )
            claims = self.claims.list(session_id, candidate_id=cand.id, limit=500)
            ev = evidence_for_candidate(cand, all_evidence, claims=claims)

            # 1. deterministic filters
            fr = self.filter_engine.evaluate(
                session_id, cand,
                evidence_records=ev,
                claims=claims,
                existing_niches=existing_niches,
            )
            if fr.verdict == "reject":
                self.candidates.set_status(session_id, cand.id, "rejected")
                self.events.append(
                    session_id, level="warn", actor="agent", action="candidate_rejected_by_filters",
                    detail={"candidate_id": cand.id, "niche": cand.niche[:120],
                            "reasons": fr.reasons[:5]},
                )
                self.trace.record(
                    session_id, KIND_CANDIDATE,
                    candidate_rejected_text(cand.niche, fr.reasons),
                )
                rejected += 1
                continue
            if fr.verdict == "downgrade":
                self.candidates.update_score(session_id, cand.id, -1.0)
                downgraded += 1

            # 2. deterministic verification
            model_verdict = self._model_verdict_for(phase_outputs, cand.niche)
            vr = self.verification_engine.verify(
                session_id, cand, evidence_records=ev, model_verdict=model_verdict,
                estimate_ids=estimate_ids,
            )
            new_status = self.verification_engine.apply(
                session_id, vr, candidates=self.candidates,
            )
            if new_status == "verified":
                accepted += 1
            elif new_status == "rejected":
                rejected += 1
            self.events.append(
                session_id, level="info", actor="agent",
                action="candidate_verification_completed",
                detail={"candidate_id": cand.id, "niche": cand.niche[:120],
                        "verdict": vr.verdict, "filters": fr.verdict,
                        "confidence": vr.confidence},
            )
            self.trace.record(
                session_id, KIND_VERIFICATION,
                verification_text(cand.niche, vr.verdict, vr.problems),
            )
            if new_status == "verified":
                self.trace.record(
                    session_id, KIND_CANDIDATE,
                    candidate_verified_text(cand.niche, vr.confidence),
                )
        self.checkpoints.save(session_id, "integrity_pipeline", {
            "accepted": accepted, "downgraded": downgraded, "rejected": rejected,
            "at": iso_now(),
        })
        self.events.append(
            session_id, level="info", actor="agent", action="integrity_pipeline_completed",
            detail={"accepted": accepted, "downgraded": downgraded, "rejected": rejected},
        )

    # ------------------------------------- durable resume cursor helpers
    def _pivot_rounds(self, session_id: str) -> int:
        """Strategic focus pivots already taken (durable — restored across
        restarts so the pivot budget never resets)."""
        try:
            cp = self.checkpoints.get_safe(session_id, "focus_pivots")
            pivots = (cp.payload or {}).get("pivots") if cp else None
            return len(pivots) if isinstance(pivots, list) else 0
        except Exception:
            return 0

    def _load_phase_output(
        self, session_id: str, phase: str, current_pass: int
    ) -> dict[str, Any] | None:
        """The phase's committed output from an earlier run of the SAME pass.

        This is the durable resume cursor between phases: an interrupted
        session reuses every phase it already committed and continues at the
        first incomplete phase instead of re-running the methodology from
        Phase 1. Checkpoints from a previous pass (before a focus pivot) are
        ignored — the new pass must not inherit the dead focus's decisions.
        """
        try:
            cp = self.checkpoints.get_safe(session_id, f"phase_output:{phase}")
        except Exception:
            return None
        if cp is None:
            return None
        payload = cp.payload or {}
        if int(payload.get("pass") or 0) != current_pass:
            return None
        out = payload.get("output")
        return out if isinstance(out, dict) and out else None

    def _load_phase_progress(
        self, session_id: str, phase: str, current_pass: int
    ) -> dict[str, Any]:
        """The phase's mid-phase cursor (last completed iteration, stall
        count, best output so far) for the CURRENT pass."""
        try:
            cp = self.checkpoints.get_safe(session_id, f"phase_progress:{phase}")
        except Exception:
            return {}
        payload = dict(cp.payload) if cp else {}
        if int(payload.get("pass") or 0) != current_pass:
            return {}
        return payload

    def _save_phase_progress(
        self, session_id: str, phase: str, *, iteration: int,
        stall: int, best_output: dict[str, Any] | None, current_pass: int,
    ) -> None:
        """Checkpoint the mid-phase resume cursor. Accounting-only: any
        failure is swallowed; research never waits on it."""
        try:
            self.checkpoints.save(
                session_id, f"phase_progress:{phase}",
                {"pass": current_pass, "iteration": iteration, "stall": stall,
                 "best_output": best_output, "at": iso_now()},
            )
        except Exception:
            pass

    def _post_p8_done(self, session_id: str) -> bool:
        """Whether Phase 8's deterministic post-phase work (integrity
        pipeline + market-memory harvest + pivot gate) finished for the
        current pass — durable, so an interrupt between the phase commit
        and that work is caught up on resume instead of skipped."""
        try:
            cp = self.checkpoints.get_safe(session_id, "post_p8")
            return bool((cp.payload or {}).get("done")) if cp else False
        except Exception:
            return False

    def _post_adversarial(
        self, session_id: str, session: Any,
        phase_outputs: dict[str, Any], pivot_round: int, session_started: float,
    ) -> bool:
        """Deterministic post-Phase-8 work. Returns True when a strategic
        focus pivot should restart the pass (its own durable record)."""
        self.trace.record(
            session_id, KIND_VERIFICATION,
            "Running the deterministic integrity pipeline: filters and "
            "verification decide which candidates may reach synthesis",
        )
        self._run_integrity_pipeline(session_id, phase_outputs)
        # Cross-session market memory: everything this pass measured
        # (verified niches, near-misses, keyword numbers, competitors,
        # census stats) is distilled into the owner-scoped knowledge base
        # so later runs reuse it. Best-effort: memory never breaks research.
        self._harvest_memory(session_id)
        # Strategic focus pivot gate: when a full deep pass leaves ZERO
        # verified candidates and the session can still afford a full pass,
        # restart discovery on a new focus with the failure on record.
        if self._maybe_strategic_pivot(
            session_id, session, pivot_round, session_started
        ):
            return True
        try:
            self.checkpoints.save(
                session_id, "post_p8",
                {"done": True, "pass": self._pivot_rounds(session_id), "at": iso_now()},
            )
        except Exception:
            pass  # marker is best-effort; the pipeline result itself is durable
        return False

    # ---------------------------------------------------- strategic pivot
    def _maybe_strategic_pivot(
        self, session_id: str, session: Any, pivot_round: int, session_started: float
    ) -> bool:
        """Strategic focus pivot gate (runs after Phase 8's integrity pipeline).

        The methodology is allowed to conclude that the whole focus is
        unviable — deep research and niche-down included. When that happens
        (zero candidates verified) and the session can still afford another
        full pass, discovery re-enters on a NEW focus. The pivot is durable
        (event + trace + 'focus_pivots' checkpoint) and its directive is
        re-injected into every prompt of the next pass. Returns True when a
        pivot pass should run.
        """
        if pivot_round >= self.config.max_focus_pivots:
            return False
        survivors = self.candidates.list(
            session_id, status="verified", limit=self.rc.max_candidates
        )
        if survivors:
            return False
        # A pivot spends a whole new P1→P9 pass: only while most of the
        # session's hard time budget is still available.
        budget = self.config.session_hard_timeout_hours * 3600
        elapsed = time.monotonic() - session_started
        remaining = budget - elapsed
        if remaining < budget * self.rc.min_remaining_session_fraction_for_pivot:
            self.events.append(
                session_id, level="info", actor="agent",
                action="focus_pivot_skipped_no_time_budget",
                detail={"round": pivot_round + 1,
                        "remaining_seconds": round(remaining, 1)},
            )
            return False

        all_cands = self.candidates.list(session_id, limit=self.rc.max_candidates)
        dead = [c for c in all_cands if c.status == "rejected"]
        unresolved = [c for c in all_cands if c.status not in ("rejected", "verified")]
        directive = focus_pivot_directive(
            round_no=pivot_round + 1,
            dead_niches=[c.niche for c in dead],
            unresolved_niches=[c.niche for c in unresolved],
            has_brief=bool((session.prompt or "").strip()),
        )
        record: dict[str, Any] = {
            "round": pivot_round + 1,
            "at": iso_now(),
            "verified": 0,
            "rejected": len(dead),
            "dead_niches": [c.niche[:120] for c in dead[:8]],
            "unresolved_niches": [c.niche[:120] for c in unresolved[:8]],
            "directive": directive,
        }
        prior = self.checkpoints.get_safe(session_id, "focus_pivots")
        pivots = list((prior.payload or {}).get("pivots") or []) if prior else []
        pivots.append(record)
        self.checkpoints.save(session_id, "focus_pivots", {"pivots": pivots})
        self.events.append(
            session_id, level="warn", actor="agent", action="focus_pivot",
            detail={
                "round": record["round"],
                "rejected": record["rejected"],
                "dead_niches": record["dead_niches"],
                "pivot_budget_remaining": self.config.max_focus_pivots - pivot_round - 1,
            },
        )
        self.trace.record(
            session_id, KIND_PIVOT,
            focus_pivot_text(record["round"], record["rejected"]),
            detail={"round": record["round"], "rejected": record["rejected"]},
        )
        return True

    def _current_pivot_directive(self, session_id: str) -> str | None:
        """The latest strategic pivot directive, or None for a normal pass.

        Read from the durable checkpoint (never run-local state) so a paused
        or restarted session keeps the pivot framing on every prompt.
        """
        cp = self.checkpoints.get_safe(session_id, "focus_pivots")
        pivots = list((cp.payload or {}).get("pivots") or []) if cp else []
        if not pivots:
            return None
        text = str(pivots[-1].get("directive") or "").strip()
        return text or None

    def _focus_pivot_history(self, session_id: str) -> list[dict[str, Any]]:
        """Durable pivot history for the final report (without directives)."""
        cp = self.checkpoints.get_safe(session_id, "focus_pivots")
        pivots = list((cp.payload or {}).get("pivots") or []) if cp else []
        out: list[dict[str, Any]] = []
        for p in pivots:
            if not isinstance(p, dict):
                continue
            out.append({
                "round": p.get("round"),
                "at": p.get("at") or "",
                "rejected": p.get("rejected", 0),
                "dead_niches": list(p.get("dead_niches") or []),
                "unresolved_niches": list(p.get("unresolved_niches") or []),
            })
        return out

    def _register_candidate_claims(
        self,
        session_id: str,
        cand: Any,
        phase_outputs: dict[str, Any],
        evidence_kinds: dict[str, str] | None = None,
    ) -> None:
        """Register the candidate's important factual claims from the durable
        phase analyses. Claims the model asserted WITHOUT evidence ids are
        registered as unsupported (quality: model_inference) — they count
        against the candidate's claim-integrity ratios rather than silently
        becoming report facts."""
        niche = cand.niche.strip().lower()
        if not niche:
            return
        existing = {
            c.statement.strip().lower()
            for c in self.claims.list(session_id, candidate_id=cand.id, limit=500)
        }

        def _claim(kind: str, statement: str, evidence_ids: list[str], interpretation: str) -> None:
            statement = (statement or "").strip()
            if not statement or statement.lower() in existing:
                return
            if len(statement) < 8:
                return
            # Chain-of-custody enforcement: cited evidence must EXIST in this
            # session. Fabricated or foreign ids are dropped deterministically.
            valid_ids = self._valid_evidence_ids(session_id, evidence_ids)
            self.claims.create(
                session_id,
                kind=kind,
                statement=statement,
                evidence_ids=valid_ids,
                candidate_id=cand.id,
                interpretation=interpretation,
                meta={"source": "phase_analysis"},
                evidence_kinds=evidence_kinds,
            )
            existing.add(statement.lower())

        def _mentions(text: str) -> bool:
            return niche[:24] in (text or "").lower()

        # Seed evidence (adjacency promotions, and any candidate created with
        # its own evidence ids): register ONE chain-of-custody claim pointing
        # at records that really exist, so a candidate born mid-run is judged
        # on captured evidence instead of on silence.
        seed_ids = [str(e) for e in ((cand.meta or {}).get("seed_evidence_ids") or [])]
        if seed_ids:
            _claim(
                "demand",
                f"Measured market signal observed for candidate: {cand.niche}",
                seed_ids,
                "seed evidence captured when this candidate was created",
            )

        for m in (phase_outputs.get("competitive_landscape") or {}).get("competitive_maps") or []:
            if isinstance(m, dict) and _mentions(str(m.get("niche", ""))):
                _claim("competition", str(m.get("saturation") or ""),
                       [str(e) for e in (m.get("evidence_ids") or [])],
                       "competitive landscape map")
        for cm in (phase_outputs.get("consumer_intelligence") or {}).get("consumer_maps") or []:
            if isinstance(cm, dict) and _mentions(str(cm.get("niche", ""))):
                for c in (cm.get("complaints") or [])[:6]:
                    _claim("consumer_need", str(c),
                           [str(e) for e in (cm.get("evidence_ids") or [])],
                           "recurring reader complaint")
        for d in (phase_outputs.get("demand_validation") or {}).get("demand_assessments") or []:
            if isinstance(d, dict) and _mentions(str(d.get("niche", ""))):
                _claim("demand", str(d.get("verdict") or ""),
                       [str(e) for e in (d.get("evidence_ids") or [])],
                       "demand validation assessment")
        for g in (phase_outputs.get("opportunity_gap") or {}).get("gap_analyses") or []:
            if isinstance(g, dict) and _mentions(str(g.get("niche", ""))):
                _claim("market_gap", str(g.get("competitors_fail") or ""),
                       [str(e) for e in (g.get("evidence_ids") or [])],
                       "opportunity-gap analysis")

    @staticmethod
    def _model_verdict_for(phase_outputs: dict[str, Any], niche: str) -> str | None:
        """The model's Phase-8 verdict for this niche, if it gave one."""
        n = niche.strip().lower()
        for v in (phase_outputs.get("adversarial_verification") or {}).get("verdicts") or []:
            if isinstance(v, dict) and str(v.get("niche", "")).strip().lower() == n:
                return str(v.get("verdict") or "").strip().lower() or None
        return None

    def _authoritative_verification_status(self, session_id: str, candidate_id: str) -> str:
        """Best deterministic verification verdict for a candidate, mapped
        onto the opportunity's verification_status field."""
        vers = VerificationStore(self.db).for_candidate(session_id, candidate_id)
        verdicts = [v.verdict for v in vers if v.verdict]
        if "verified" in verdicts:
            return "verified"
        if "verified_with_limitations" in verdicts:
            return "verified_with_limitations"
        if "rejected" in verdicts:
            return "rejected"
        if "inconclusive" in verdicts:
            return "inconclusive"
        return "adversarially verified"  # Phase-8-only fallback (pre-pipeline)

    def _valid_evidence_ids(self, session_id: str, evidence_ids: list[str]) -> list[str]:
        """Filter a model-supplied evidence-id list down to ids that actually
        exist in this session (browser evidence + research evidence)."""
        if not evidence_ids:
            return []
        known = {
            e.id for e in self.browser_evidence.list(session_id, limit=5000)
        } | {
            e.id for e in self.evidence.list(session_id, limit=5000)
        }
        return [str(e) for e in evidence_ids if str(e) in known]

    def _all_browser_evidence(self, session_id: str) -> list[dict[str, Any]]:
        return [
            {
                "id": e.id,
                "kind": e.kind,
                "marketplace": e.marketplace,
                "title": e.title,
                "url": e.url,
                "data": e.data,
                "captured_at": e.captured_at,
            }
            for e in self.browser_evidence.list(session_id, limit=2000)
        ]

    # ------------------------------------------------------------------ digests
    def _depth_note(self, session_started: float) -> str:
        """One deterministic line telling the model how much research time is
        left to spend.

        Depth is a backend decision (raised floors do the enforcing), but the
        model must know that committing while budget remains wastes hours the
        session still has — so it opens pages it has not opened instead of
        re-running searches and stalling.
        """
        try:
            min_hours = float(getattr(self.config, "research_min_hours", 0) or 0)
        except (TypeError, ValueError):
            return ""
        if min_hours <= 0:
            return ""
        elapsed = max(0.0, time.monotonic() - session_started)
        remaining = min_hours * 3600 - elapsed
        if remaining <= 0:
            return ""
        hard = max(1, int(getattr(self.config, "session_hard_timeout_hours", 12)))
        mins = max(1, int(-(-remaining // 60)))
        return (
            f"The session is {max(1, int(elapsed // 60))} min into its research "
            f"and still owes depth: {mins} min of the {min_hours:g}-hour minimum "
            f"remain, inside a {hard}-hour budget. Until that minimum is met the "
            "exit gate demands DEEPER evidence — new sources, competitor product "
            "pages, reader reviews, category pages you have not opened — never "
            "the same searches again. Widen and read before considering a commit."
        )

    def _latest_screenshot(self) -> bytes | None:
        """Newest browser screenshot bytes for vision input (may be None)."""
        getter = getattr(self.executor.collector, "last_screenshot", None)
        return getter if isinstance(getter, (bytes, type(None))) else None

    def _evidence_digest(self, session_id: str) -> list[dict[str, Any]]:
        """Compact, model-safe digest of recent browser evidence."""
        items: list[dict[str, Any]] = []
        for e in self.browser_evidence.list(
            session_id, limit=self.rc.max_evidence_digest_items
        ):
            items.append({
                "id": e.id,
                "kind": e.kind,
                "marketplace": e.marketplace,
                "title": e.title[:120],
                "url": e.url[:160],
                "data": e.data,
                "captured_at": e.captured_at,
            })
        return items

    def _candidates_digest(self, session_id: str) -> list[dict[str, Any]]:
        """Candidates for the prompt, ordered by headroom (demand ÷
        competition) so the model spends its iterations on the highest-
        headroom candidates first. Ordering only — every candidate stays in
        the list and no verdict changes."""
        rows = self.candidates.list(session_id, limit=self.rc.max_candidates)
        ws = self._ws_scores.get(session_id) or {}
        items: list[dict[str, Any]] = []
        for c in rows:
            score = ws.get(c.id)
            value = (
                score.to_dict().get("score") if score is not None
                else (c.meta or {}).get("whitespace", {}).get("score")
            )
            items.append({
                "candidate_id": c.id,
                "niche": c.niche,
                "status": c.status,
                "marketplace": c.marketplace,
                "score": c.score,
                "whitespace": value,
                "rationale": c.rationale[:200],
            })
        items.sort(key=lambda d: (
            -(d["whitespace"] if isinstance(d["whitespace"], (int, float)) else -1.0),
            d["niche"],
        ))
        return items

    def _progress_signature(self, session_id: str) -> dict[str, int]:
        be_count = self.browser_evidence.count(session_id)
        ev_count = len(self.evidence.list(session_id, limit=1))
        cand_count = len(self.candidates.list(session_id, limit=500))
        return {
            "browser_evidence": be_count,
            "evidence_total": be_count + ev_count,
            # Distinct captured sources — the anti-stall signal (duplicates
            # of a query already researched do not raise it).
            "distinct_evidence": self.browser_evidence.distinct_sources(session_id),
            "candidates": cand_count,
        }

    def _output_floor(
        self, phase: str, prior_outputs: dict[str, Any], candidates_seen: int
    ) -> int:
        """How many primary-output items this phase needs before it may commit.

        The floor is declared per phase in the methodology (min_output_items)
        and gated on the phase's UPSTREAM INPUT, so a phase is never forced to
        manufacture content it has nothing to work with:
        - discovery has no upstream input → always needs a signal,
        - niching needs Phase 1's signals (or candidates already on record),
        - analysis phases need at least one candidate,
        - adversarial verification and synthesis declare 0 (the deterministic
          pipeline and the opportunity contract decide instead of the model).
        """
        spec = SPECS[phase]
        want = spec.min_output_items
        if want <= 0:
            return 0
        needs = set(spec.consumes)
        if not needs:
            return want
        if "signal" in needs:
            signals = (prior_outputs.get(P1_DISCOVERY) or {}).get("signals")
            return want if (signals or candidates_seen) else 0
        if "discovered" in needs:
            return want if candidates_seen else 0
        return want

    @staticmethod
    def _item_floor(spec: Any, floor: int) -> int:
        """Items a phase owes before it may commit.

        A phase that DECLARES content (`min_output_items > 0`) may never
        commit an empty primary list, even when its upstream input is missing
        — an empty hand-over is what left the report's niche, demand and
        consumer lists blank. The floor can only ever be at least 1 there;
        phases 8 and 9 (min 0) stay free, because the deterministic pipeline
        and the opportunity contract decide those instead of the model.
        """
        if getattr(spec, "min_output_items", 0) > 0:
            return max(int(floor), 1)
        return int(floor)

    def _phase_gate(
        self,
        phase: str,
        *,
        baseline: dict[str, int],
        latest: dict[str, int],
        prior_outputs: dict[str, Any],
        session_started: float = 0.0,
    ) -> dict[str, Any]:
        """Deterministic exit-gate snapshot for the model's prompt.

        The floors shown here are the LIVE floors: while the session is still
        under its minimum research duration the evidence requirement is the
        depth-raised one, so the model sees exactly how much more research
        the phase owes before it may close.
        """
        spec = SPECS[phase]
        primary = spec.output_contract[0]
        evidence_new = max(0, latest["evidence_total"] - baseline["evidence_total"])
        required = max(
            0, self._evidence_floor(spec.min_evidence, session_started) - evidence_new
        )
        return {
            "primary_output": primary,
            "min_output_items": self._item_floor(
                spec, self._output_floor(phase, prior_outputs, latest["candidates"])
            ),
            "evidence_new": evidence_new,
            "evidence_required": required,
            "duplicate_sources_do_not_count": True,
        }

    def _has_images(self, session_id: str) -> bool:
        from app.uploads import UploadStore

        return bool(UploadStore(self.db, self.config).list(session_id))

    def _reference_images(self, session_id: str) -> list[bytes]:
        """Owner reference images, prepared for the model — cached per run.

        Image-only and image-steered sessions only work if the model can
        actually SEE the uploads: a text note saying "images were uploaded"
        cannot steer a six-hour run. Each image is downscaled to a bounded
        JPEG once (a 20 MB phone photo must never bloat every request) and
        then attached to each model call of this run. Loading is best-effort:
        a corrupt or missing upload simply drops out of the prompt — images
        may only ever improve research, never break it.
        """
        cached = self._ref_images.get(session_id)
        if cached is not None:
            return cached
        images: list[bytes] = []
        try:
            if self.config.model_vision_enabled:
                from app.uploads import UploadStore

                store = UploadStore(self.db, self.config)
                for up in store.list(session_id)[: self.rc.max_reference_images]:
                    try:
                        prepared = self._prepare_reference_image(store.read_bytes(up))
                    except Exception:
                        continue
                    if prepared:
                        images.append(prepared)
        except Exception:
            images = []
        self._ref_images[session_id] = images
        return images

    @staticmethod
    def _prepare_reference_image(data: bytes) -> bytes | None:
        """Downscale/re-encode an uploaded image to a bounded JPEG.

        Returns None for anything unprocessable — transparency is flattened
        onto white, animated formats contribute their first frame only.
        """
        try:
            from io import BytesIO

            from PIL import Image

            img = Image.open(BytesIO(data))
            try:
                img.seek(0)  # animated formats: first frame only
            except (EOFError, AttributeError):
                pass
            if img.mode in ("RGBA", "LA", "P", "PA"):
                rgba = img.convert("RGBA")
                bg = Image.new("RGB", rgba.size, (255, 255, 255))
                bg.paste(rgba, mask=rgba.getchannel("A"))
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")
            img.thumbnail((768, 768))
            out = BytesIO()
            img.save(out, format="JPEG", quality=85)
            return out.getvalue()
        except Exception:
            return None

    # ------------------------------------------------------------------ helpers
    def _resolve_marketplaces(self, session: Any) -> tuple[list[Marketplace], str]:
        """Explicit lists are obeyed exactly; otherwise auto-select by
        relevance to the brief (~4-5 marketplaces)."""
        if session.marketplaces:
            mode, marketplaces = resolve_plan(session.marketplaces)
            return marketplaces, mode
        return select_auto(session.prompt or "", session.objective or ""), "auto"

    def _marketplace_mode(self, session_id: str) -> str:
        cp = self.checkpoints.get(session_id, "marketplace_plan")
        return str(cp.payload.get("mode")) if cp else "auto"

    def _find_candidate_by_niche(self, session_id: str, niche: str):
        for c in self.candidates.list(session_id, limit=200):
            if c.niche.strip().lower() == niche.strip().lower():
                return c
        return None

    # ------------------------------------------------------------- failure paths
    def _mark_job(self, session_id: str, status: str, detail: dict[str, Any] | None = None) -> None:
        """Advance the durable research-job row through its autonomy states.

        Jobs are created by the start/resume routes and boot recovery; the
        supervisor owns the vocabulary (queued → initializing → researching →
        completed / failed / waiting_authentication / …). Without these marks
        the durable job row — and the `job_*` events on the owner's feed —
        froze at its creation state forever: a finished session still showed
        a `pending` job. Best-effort: job bookkeeping never blocks research.
        """
        mark = getattr(self.supervisor, "mark", None)
        if mark is None:
            return
        try:
            mark(session_id, status, detail=detail or None)
        except Exception:
            pass  # autonomy bookkeeping is additive; research proceeds

    def _pause_for_auth(self, session_id: str, exc: AuthPauseRequired) -> None:
        self.sessions.update(session_id, status="paused", error=f"authentication required: {exc}")
        self.agent.transition(session_id, "waiting_recovery", substate="auth_required")
        self._mark_job(session_id, "waiting_authentication", {"note": str(exc)[:200]})
        self.errors.record(
            session_id, scope="browser_auth", severity="warning",
            message=str(exc), recoverable=True,
        )
        self.events.append(
            session_id, level="warn", actor="browser", action="paused_for_auth",
            detail=exc.detail,
        )
        self.checkpoints.save(
            session_id, "paused:auth", {"reason": str(exc), "detail": exc.detail, "at": iso_now()},
        )

    def _pause_for_model(self, session_id: str, exc: Exception) -> None:
        self.sessions.update(session_id, status="paused", error=f"model failure: {exc}")
        self.agent.transition(session_id, "waiting_recovery", substate="model_failure")
        self._mark_job(session_id, "paused", {"note": f"model failure: {str(exc)[:160]}"})
        self.errors.record(
            session_id, scope="model", severity="error",
            message=str(exc), recoverable=True,
        )
        self.events.append(
            session_id, level="error", actor="model",
            action="paused_for_model_failure", detail={"error": str(exc)[:300]},
        )
        self.checkpoints.save(session_id, "paused:model", {"reason": str(exc)[:500], "at": iso_now()})

    def _pause_for_browser(self, session_id: str, exc: BrowserManagerError) -> None:
        self.sessions.update(session_id, status="paused", error=f"browser failure: {exc}")
        self.agent.transition(session_id, "waiting_recovery", substate="browser_failure")
        self._mark_job(session_id, "paused", {"note": f"browser failure: {str(exc)[:160]}"})
        self.errors.record(
            session_id, scope="browser", severity="error", message=str(exc), recoverable=True,
        )
        self.events.append(
            session_id, level="error", actor="browser",
            action="paused_for_browser_failure", detail={"error": str(exc)[:300]},
        )

    def _self_heal(self, session_id: str, exc: Exception, remaining: int) -> None:
        """Recover an unexpected research error WITHOUT failing the session.

        The retry re-enters the methodology at its durable checkpoint
        cursor: completed phases are reused from 'phase_output', the
        interrupted phase resumes from its own iteration checkpoint, and
        discovery preflight is never re-walked — so a heal costs one extra
        attempt, never research quality. Every heal is recorded durably
        (recoverable error state, event, recovery trace, checkpoint) so the
        owner can audit exactly what was recovered; the session fails only
        when the heal budget is spent (and stays owner-resumable even then).
        """
        try:
            # Park the agent in waiting_recovery so the retry's
            # methodology_start transition is legal from ANY mid-phase state.
            self.agent.transition(session_id, "waiting_recovery", substate="self_heal")
        except Exception:
            pass  # already there (or a DB hiccup) — the heal proceeds anyway
        try:
            self.errors.record(
                session_id, scope="runner", severity="warning",
                message=f"{type(exc).__name__}: {exc}", recoverable=True,
            )
            self.events.append(
                session_id, level="warn", actor="system",
                action="research_self_heal",
                detail={
                    "error": str(exc)[:300],
                    "type": type(exc).__name__,
                    "attempts_remaining": remaining,
                    "trace": traceback.format_exc()[-800:],
                },
            )
            self.trace.record(
                session_id, KIND_RECOVERY,
                recovery_text(
                    f"{type(exc).__name__}: {str(exc)[:100]}",
                    f"resuming from the last checkpoint "
                    f"({remaining} recovery attempt(s) left)",
                ),
            )
            self.checkpoints.save(
                session_id, "self_heal",
                {"at": iso_now(), "error": str(exc)[:500],
                 "type": type(exc).__name__, "remaining": remaining},
            )
            self._mark_job(
                session_id, "recovering",
                {"note": f"self-heal: {str(exc)[:160]}"},
            )
        except Exception:
            pass  # audit bookkeeping never blocks the recovery itself

    def _fail(self, session_id: str, exc: Exception) -> None:
        self.sessions.update(session_id, status="failed", error=str(exc)[:500])
        self.agent.transition(session_id, "failed", substate="runner_error")
        self._mark_job(session_id, "failed", {"note": f"{type(exc).__name__}: {str(exc)[:200]}"})
        self.errors.record(
            session_id, scope="runner", severity="critical",
            message=f"{type(exc).__name__}: {exc}", recoverable=False,
        )
        self.events.append(
            session_id, level="error", actor="system", action="research_failed",
            detail={"error": str(exc)[:300], "trace": traceback.format_exc()[-800:]},
        )

    async def _analyst_narrative(self, session_id: str, model: Any) -> dict[str, Any]:
        """Model-written, deterministically grounded analyst prose.

        The backend owns every accounting figure; the model only
        interprets the verified digest (app.report_narrative). Unknown
        numbers cause one corrective retry and are then stripped — an
        invented figure never reaches the report. ANY failure degrades to
        the deterministic report: the narrative is additive, never a
        dependency, and a session must complete without it.
        """
        from app.report_narrative import (
            NARRATIVE_FIELDS,
            NARRATIVE_SYSTEM,
            allowed_numbers,
            build_digest,
            correction_prompt,
            digest_json,
            normalize_narrative,
            user_prompt,
        )

        try:
            data = digest_json(build_digest(model.to_dict()))
        except Exception:
            return {}
        allowed = allowed_numbers(data)
        prompt = user_prompt(data)
        narrative: dict[str, Any] = {}
        unresolved: list[str] = []
        for attempt in (1, 2):
            try:
                reply = await self.model.call(
                    session_id,
                    system=NARRATIVE_SYSTEM,
                    user=prompt,
                    expect_fields=NARRATIVE_FIELDS,
                    label="Writing the analyst report",
                )
            except Exception as exc:  # CancelledError is BaseException — passes
                self.events.append(
                    session_id, level="warn", actor="agent",
                    action=(
                        "report_narrative_unavailable" if attempt == 1
                        else "report_narrative_retry_failed"
                    ),
                    detail={"error": f"{type(exc).__name__}: {exc}"[:300]},
                )
                break  # keep a previously grounded narrative, else deterministic
            try:
                self._record_model_usage(session_id, reply)
            except Exception:
                pass
            narrative, unresolved = normalize_narrative(
                getattr(reply, "content", None), allowed=allowed
            )
            if narrative and not unresolved:
                break
            if attempt == 1:
                prompt = correction_prompt(
                    data, unresolved or ["an unusable section structure"]
                )
        if not narrative:
            self.events.append(
                session_id, level="info", actor="agent",
                action="report_narrative_skipped",
                detail={"unresolved_figures": len(unresolved)},
            )
            return {}
        self.events.append(
            session_id, level="info", actor="agent",
            action="report_narrative_written",
            detail={
                "sections": sorted(narrative.keys()),
                "opportunities_analyzed": len(
                    narrative.get("opportunity_analyses") or {}
                ),
                "figures_removed": len(unresolved),
            },
        )
        return narrative

    # -------------------------------------- final-candidate deep dive
    def _final_candidates(self, session_id: str) -> list[Any]:
        """The candidates that actually reach this report, in report order.

        Opportunity dossiers lead (they ARE the report's portfolio), then
        every other verified survivor — the candidate ledger prints those
        too, so they get the same measured keywords and category maps.
        Rejected and still-unverified candidates never do: they are not
        final. Capped by `dive_max_candidates` so the walk stays bounded.
        """
        cands = {c.id: c for c in self.candidates.list(session_id, limit=500)}
        ordered: list[Any] = []
        seen: set[str] = set()
        for o in self.opportunities.list(session_id, limit=50):
            c = cands.get(o.candidate_id)
            if c is not None and c.id not in seen:
                seen.add(c.id)
                ordered.append(c)
        for c in cands.values():
            if c.id in seen or c.status != CAND_VERIFIED:
                continue
            seen.add(c.id)
            ordered.append(c)
        cap = int(getattr(self.config, "dive_max_candidates", 8))
        return ordered[:max(1, cap)]

    async def _final_deep_dive(self, session_id: str) -> None:
        """Measure backend keywords + both store category maps for every
        candidate that will appear in the report.

        Deterministic (no model) and durable: each finalist's dive is
        checkpointed under `final_dive:<candidate_id>`, so a resumed report
        reuses finished dives and never re-walks. A failure — budget, pause,
        a page that will not open — degrades to fewer measured rows and is
        journaled; it never blocks the report itself.
        """
        cfg = self.config
        if not getattr(cfg, "dive_enabled", True):
            return
        targets = self._final_candidates(session_id)
        if not targets:
            return
        pending: list[Any] = []
        for cand in targets:
            try:
                cp = self.checkpoints.get_safe(session_id, f"final_dive:{cand.id}")
                payload = (cp.payload or {}) if cp else {}
            except Exception:
                payload = {}
            if payload.get("done"):
                continue
            pending.append(cand)
        if not pending:
            return
        try:
            session = self.sessions.require(session_id)
            marketplaces, _mode = self._resolve_marketplaces(session)
        except Exception as exc:
            self.events.append(
                session_id, level="warn", actor="system",
                action="final_dive_skipped",
                detail={"error": f"{type(exc).__name__}: {exc}"[:200]},
            )
            return
        max_kw = int(getattr(cfg, "dive_max_keywords", 20))
        max_cat = int(getattr(cfg, "dive_max_categories", 10))
        self.events.append(
            session_id, level="info", actor="system", action="final_dive_started",
            detail={
                "candidates": [c.id for c in pending],
                "max_keywords_per_candidate": max_kw,
                "max_categories_per_store": max_cat,
                "stores": ["kindle_store", "books"],
            },
        )
        self.trace.record(
            session_id, KIND_DISCOVERY,
            f"Finalist deep dive: measuring up to {max_kw} backend keywords and "
            f"up to {max_cat} categories each for the Kindle Store and the Books "
            f"store on {len(pending)} report candidate(s) — measured, not guessed",
        )
        dive = FinalCandidateDive(
            self.browser,
            self.browser_evidence,
            self.events,
            max_keywords=max_kw,
            max_categories=max_cat,
            autocomplete_queries=int(getattr(cfg, "dive_autocomplete_queries", 24)),
            products_per_store=int(getattr(cfg, "dive_products_per_store", 6)),
            time_budget_seconds=float(getattr(cfg, "dive_time_budget_seconds", 300)),
            should_stop=lambda: self._cancelled or self._pause_requested,
        )
        wrote_evidence = False
        for cand in pending:
            if self._cancelled or self._pause_requested:
                break
            res = await dive.run(
                session_id=session_id,
                marketplaces=marketplaces,
                niche=cand.niche,
                candidate_id=cand.id,
            )
            if res.evidence_ids:
                wrote_evidence = True
            try:
                self.checkpoints.save(
                    session_id, f"final_dive:{cand.id}",
                    {"done": bool(res.done), **res.to_dict()},
                )
            except Exception:
                pass  # the measured evidence is already durable
            self.events.append(
                session_id,
                level="warn" if res.error else "info",
                actor="system",
                action=(
                    "final_dive_candidate_failed" if res.error
                    else "final_dive_candidate_completed"
                ),
                detail={
                    "candidate_id": cand.id,
                    "niche": cand.niche[:120],
                    "keywords_measured": res.keywords_measured,
                    "books_categories": len(res.books_categories),
                    "kindle_categories": len(res.kindle_categories),
                    "stopped_reason": res.stopped_reason,
                    **({"error": res.error} if res.error else {}),
                },
            )
            if res.error:
                self.errors.record(
                    session_id, scope="final_dive", severity="warning",
                    message=res.error[:300], recoverable=True,
                )
        if wrote_evidence:
            # The normal memory harvest ran BEFORE this dive (it is part of
            # the post-Phase-8 work). Distill the new measurements too, so
            # the next session never has to pay for them again.
            self._harvest_memory(session_id)

    async def _write_final_report(self, session_id: str, phase_outputs: dict[str, Any]) -> None:
        from app.quality_gates import QualityGateRunner
        from app.report_model import build_report_model, render_markdown
        from app.report_validation import ReportValidator
        from app.research_data import VerificationStore

        # Finalist deep dive FIRST: every candidate that reaches this report
        # gets its backend keywords measured and BOTH store category maps
        # (Kindle Store and Books are different category systems). The
        # results are ordinary evidence, so the model below interprets them
        # instead of inventing them. It degrades — never blocks — the report.
        try:
            await self._final_deep_dive(session_id)
        except Exception as exc:
            self.errors.record(
                session_id, scope="final_dive", severity="warning",
                message=f"{type(exc).__name__}: {exc}"[:300], recoverable=True,
            )

        # Structured model first — the report is a RENDERING of durable
        # research state, never a free-form string.
        # Headroom scores + backlog feed the report's ranking and its
        # "adjacent opportunities not yet researched" ledger.
        try:
            ws = self._whitespace_scores(session_id)
        except Exception:
            ws = {}
        model = build_report_model(
            session_id,
            sessions=self.sessions,
            candidates=self.candidates,
            opportunities=self.opportunities,
            verifications=VerificationStore(self.db),
            claims=self.claims,
            browser_evidence=self.browser_evidence,
            observations=self.observations,
            assessments=self.assessments,
            phase_outputs=phase_outputs,
            focus_pivots=self._focus_pivot_history(session_id),
            whitespace_by_candidate={
                k: v.to_dict() for k, v in ws.items()
            },
            backlog=self._backlog_items(session_id),
            adjacency=self._adjacency_rows(session_id),
            evidence=self.evidence,
        )
        # Analyst prose: the model interprets the verified records above;
        # it never computes a figure. Empty on any failure — the report
        # renders deterministically without it.
        narrative = await self._analyst_narrative(session_id, model)
        if narrative:
            model.narrative = narrative
        body = render_markdown(model)

        # Adversarial validation of the deliverable: gates + per-opportunity
        # chain-of-custody checks. A failing report is delivered as
        # 'provisional' with the failures IN the report — never silent.
        gate_runner = QualityGateRunner(
            self.thresholds,
            self.claims,
            self.candidates,
            VerificationStore(self.db),
            self.opportunities,
            self.assessments,
            browser_evidence_count=self.browser_evidence.count(session_id),
            observation_count=self.observations.count(session_id),
            evidence_stores=(self.browser_evidence, self.evidence),
        )
        validation = ReportValidator(
            self.claims, self.opportunities, VerificationStore(self.db),
            gate_runner, self.assessments,
            candidates=self.candidates,
            browser_evidence=self.browser_evidence,
            evidence=self.evidence,
        ).validate(session_id, report_body=body)

        # The validation verdict lives in the model too, so re-renders keep it.
        model.validation = validation.to_dict()
        badge = (
            "**Report status: FINAL** — passed adversarial validation."
            if validation.passed
            else (
                "**Report status: PROVISIONAL** — adversarial validation found issues:\n"
                + "\n".join(f"- {f}" for f in validation.failures[:10])
            )
        )
        body = body.replace(
            "## Executive summary",
            f"{badge}\n\n## Executive summary",
            1,
        )
        store = ReportStore(self.db, self.config)
        rep = store.save(
            session_id,
            kind="final",
            title="Resurrección Market Intelligence Report",
            body_markdown=body,
            meta={
                "generated_by": "research_runner",
                "generator": "report_model.v2",
                "validation": validation.to_dict(),
                "mode": validation.mode,
            },
        )
        # Persist the structured model: the report remains re-renderable.
        try:
            store.save_model_json(session_id, report=rep, model_json=model.to_json())
        except Exception:
            pass  # rendering already persisted; model persistence is additive
        self.events.append(
            session_id, level="info", actor="agent", action="final_report_written",
            detail={"mode": validation.mode, "failures": len(validation.failures)},
        )


class _Cancelled(Exception):
    pass


class _NicheProbe:
    """Minimal candidate-shaped object for scoring a measured neighbour
    before (or without) it becoming a real candidate row."""

    def __init__(self, niche: str, marketplace: str = "") -> None:
        self.id = ""
        self.niche = niche
        self.marketplace = marketplace
        self.status = "discovered"
        self.meta = {}
        self.rationale = ""
        self.score = 0.0


class _RunnerAuth:
    """AmazonAuthManager handle bound to this runner's stores (lazy-wired)."""

    def __init__(
        self,
        config: Config,
        browser: BrowserManager,
        auth_store: BrowserAuthStore,
        windows: LoginWindowStore,
    ) -> None:
        from app.amazon_auth import AmazonAuthManager

        self._mgr = AmazonAuthManager(config, browser, auth_store, windows)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._mgr, name)
