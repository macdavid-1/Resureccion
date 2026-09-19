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
from dataclasses import dataclass
from typing import Any

from app.actions import ActionError, ActionExecutor, AuthPauseRequired
from app.agent import AgentStateStore
from app.artifacts import ArtifactStore
from app.browser_manager import BrowserManager, BrowserManagerError
from app.browser_store import BrowserAuthStore, BrowserEvidenceStore, LoginWindowStore
from app.claims import ClaimStore, EQ_INFERENCE
from app.config import Config
from app.db import Database
from app.events import EventLog
from app.evidence_capture import EvidenceCollector
from app.filters import CandidateFilterEngine
from app.integrity import IntegrityAssessmentStore
from app.kdp_risk import record_keyword_screening, screen_keywords
from app.marketplace import Marketplace, resolve_plan, select_auto
from app.methodology import (
    P1_DISCOVERY,
    PHASE_ORDER,
    PHASE_TITLES,
    SESSION_PHASE_BY_METHOD_PHASE,
    SPECS,
    STALL_LIMIT,
    PhaseTiming,
    validate_opportunity,
)
from app.methodology import P8_ADVERSARIAL
from app.model_client import (
    ModelClient,
    ModelConfigError,
    ModelContractError,
    ModelError,
)
from app.prompts import build_system_prompt, build_user_prompt
from app.recovery import CheckpointStore, ErrorStateStore
from app.quality_gates import QualityGateRunner
from app.reports import ReportStore
from app.research_data import (
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
    KIND_PHASE,
    KIND_PIVOT,
    KIND_REPORT,
    KIND_VERIFICATION,
    ActivityTrace,
    browser_text,
    candidate_created_text,
    candidate_rejected_text,
    candidate_verified_text,
    marketplace_text,
    phase_text,
    pivot_text,
    verification_text,
)
from app.verification import VerificationEngine, evidence_for_candidate


@dataclass
class RunnerConfig:
    """Runner-level tunables (deterministic; not model-visible)."""

    max_model_calls_per_session: int = 120      # absolute safety ceiling
    max_evidence_digest_items: int = 40         # digest size cap per prompt
    max_candidates: int = 24                    # candidates tracked at once
    min_opportunities_target: int = 5           # target for auto sessions
    max_opportunities: int = 12                 # synthesis output cap


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
    ) -> None:
        self.db = db
        self.config = config
        self.sessions = sessions
        self.agent = agent
        self.events = events
        self.errors = errors
        self.checkpoints = checkpoints
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
        self.supervisor = supervisor
        self._pause_requested = False
        self._timings: dict[str, TimingTracker] = {}
        self.executor = executor or ActionExecutor(
            config,
            browser,
            EvidenceCollector(browser, browser_evidence, artifacts),
            _RunnerAuth(config, browser, browser_auth, LoginWindowStore(db)),
            events,
        )
        self._cancelled = False

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

    # ------------------------------------------------------------------- run
    async def run(self, session_id: str) -> None:
        """Execute the full methodology for a session. Never raises outward —
        every failure path ends in a durable session status."""
        session = self.sessions.require(session_id)
        session_started = time.monotonic()
        try:
            self.sessions.update(session_id, status="running", error=None)
            # Idempotent: after a crash/resume the agent state row already
            # exists; a fresh session needs it created before transitioning.
            self.agent.initialize(session_id)
            self.agent.transition(session_id, "planning", substate="methodology_start")
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

            # 1-9. The methodology.
            phase_outputs: dict[str, Any] = {}
            timings = self._timings_for(session_id)
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
                output = await self._run_phase(
                    session_id, session, phase, phase_outputs, session_started
                )
                phase_outputs[phase] = output
                self.checkpoints.save(
                    session_id, f"phase_complete:{phase}",
                    {"completed_at": iso_now(), "output_keys": sorted(output.keys())},
                )
                timings.finish(f"phase:{phase}", outcome="ok")
                if self.supervisor is not None:
                    self.supervisor.heartbeat(session_id, phase=phase, note="phase_complete")
                self.sessions.update(
                    session_id, phase=SESSION_PHASE_BY_METHOD_PHASE.get(phase, "exploration")
                )
                # After adversarial verification (P8), the deterministic
                # integrity pipeline filters and verifies every synthesis-bound
                # candidate BEFORE synthesis may run.
                if phase == P8_ADVERSARIAL:
                    self.trace.record(
                        session_id, KIND_VERIFICATION,
                        "Running the deterministic integrity pipeline: filters and "
                        "verification decide which candidates may reach synthesis",
                    )
                    self._run_integrity_pipeline(session_id, phase_outputs)

            self.agent.transition(session_id, "reporting", substate="synthesis_done")
            self.trace.record(session_id, KIND_REPORT, "Generating the final research report")
            self._write_final_report(session_id, phase_outputs)

            self.sessions.update(session_id, status="completed", progress=1.0)
            self.agent.transition(session_id, "done")
            self.events.append(
                session_id, level="info", actor="agent", action="research_completed",
                detail={"elapsed_seconds": round(time.monotonic() - session_started, 1)},
            )
        except _Cancelled:
            try:
                self.sessions.update(session_id, status="cancelled", error="cancelled by owner")
                self.agent.transition(session_id, "stopped", substate="cancelled")
            except Exception:
                pass  # never mask a durable cancel behind a DB hiccup
        except AuthPauseRequired as exc:
            self._pause_for_auth(session_id, exc)
        except (ModelConfigError, ModelError) as exc:
            self._pause_for_model(session_id, exc)
        except BrowserManagerError as exc:
            self._pause_for_browser(session_id, exc)
        except asyncio.CancelledError:
            self.sessions.update(session_id, status="interrupted", error="runner task cancelled")
            self.agent.transition(session_id, "waiting_recovery", substate="task_cancelled")
            raise
        except Exception as exc:
            self._fail(session_id, exc)
        finally:
            await self.model.aclose()

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
        phase_start = time.monotonic()
        timing = PhaseTiming(phase=phase, started_at=iso_now())
        timings = self._timings_for(session_id)
        timings.start(f"phase:{phase}", TaskType.PHASE)
        self.agent.transition(session_id, spec.agent_states[0], substate=phase)
        self.events.append(
            session_id, level="info", actor="agent", action="phase_started",
            detail={"phase": phase, "max_iterations": spec.max_iterations},
        )
        self.trace.record(
            session_id, KIND_PHASE,
            phase_text(PHASE_TITLES.get(phase, phase), entering=True),
        )

        stall = 0
        model_calls = 0
        last_counts = self._progress_signature(session_id)
        for iteration in range(1, spec.max_iterations + 1):
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
            if model_calls >= self.rc.max_model_calls_per_session:
                self.events.append(
                    session_id, level="warn", actor="agent",
                    action="session_model_budget_reached", detail={"phase": phase},
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
                    "reference images were uploaded; treat their subject matter "
                    "as part of the brief" if self._has_images(session_id) else ""
                ),
                screenshot_attached=screenshot_png is not None,
            )
            model_calls += 1
            self.agent.advance_iteration(session_id)
            self.agent.add_budget(session_id, time.monotonic() - phase_start if iteration == 1 else 0.0)
            # Per-call stuck detection: a hung model call is aborted at the
            # type-specific hard timeout and treated as a failed iteration
            # (retry budget), never a hung session.
            call_key = f"model:{phase}:{iteration}"
            timings.start(call_key, TaskType.MODEL_CALL)
            try:
                reply = await self.model.call(
                    session_id, system=system, user=user,
                    expect_fields=spec.output_contract,
                    screenshot_png=screenshot_png,
                )
                timings.finish(call_key, outcome="ok")
            except asyncio.CancelledError:
                timings.abandon(call_key, outcome="cancelled")
                raise
            except BaseException:
                timings.finish(call_key, outcome="failed")
                raise
            finally:
                verdict = timings.check(call_key)
                if verdict.hard_stuck:
                    self.events.append(
                        session_id, level="warn", actor="agent",
                        action="model_call_hard_timeout",
                        detail=verdict.to_dict(),
                    )
            if reply.elapsed_seconds and reply.elapsed_seconds >= 150:
                self.events.append(
                    session_id, level="info", actor="agent",
                    action="model_call_slow", detail={"phase": phase, "seconds": round(reply.elapsed_seconds, 1)},
                )
            # Operator-facing rationale line for this iteration (the model is
            # asked for a one-line activity_note; absent is fine).
            iter_note = str(reply.content.get("activity_note") or "").strip()
            if iter_note:
                self.trace.record(session_id, KIND_DISCOVERY, iter_note)

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
            progressed = counts != last_counts
            last_counts = counts
            stall = 0 if progressed else stall + 1
            timing.attempts_since_progress = stall

            gates_ok = not missing and (
                spec.min_evidence == 0 or counts["evidence_total"] >= spec.min_evidence
            )
            if gates_ok and stall < STALL_LIMIT:
                self.checkpoints.save(
                    session_id, f"phase_output:{phase}",
                    {"output": reply.content, "model": reply.model,
                     "iterations": iteration, "at": iso_now()},
                )
                self.events.append(
                    session_id, level="info", actor="agent", action="phase_completed",
                    detail={"phase": phase, "iterations": iteration,
                            "elapsed_seconds": round(time.monotonic() - phase_start, 1)},
                )
                self._apply_phase_effects(session_id, phase, reply.content)
                return reply.content

            if stall >= STALL_LIMIT:
                self.events.append(
                    session_id, level="warn", actor="agent", action="phase_stalled",
                    detail={"phase": phase, "iterations": iteration,
                            "missing_fields": missing,
                            "evidence_total": counts["evidence_total"]},
                )
                self.trace.record(
                    session_id, KIND_PIVOT,
                    pivot_text(PHASE_TITLES.get(phase, phase), f"no new evidence after {iteration} iterations; forcing the phase to contribute what it has and moving on"),
                )
                # Force-apply whatever analysis the model produced so the phase
                # still contributes, then move on.
                self._apply_phase_effects(session_id, phase, reply.content)
                break

        # Budget exhausted or stalled: record and continue with what we have.
        self.errors.record(
            session_id, scope=f"methodology:{phase}", severity="warning",
            message=f"phase {phase} ended at budget/stall without full gate pass",
            recoverable=True,
        )
        return {}

    # ------------------------------------------------------------ phase effects
    def _apply_phase_effects(self, session_id: str, phase: str, output: dict[str, Any]) -> None:
        """Persist the durable effects of a phase's analysis."""
        # Niching produces/updates candidate rows.
        niches = output.get("niches")
        if isinstance(niches, list):
            for n in niches[: self.rc.max_candidates]:
                if not isinstance(n, dict) or not str(n.get("niche") or "").strip():
                    continue
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
        # Adversarial verdicts move candidate statuses.
        verdicts = output.get("verdicts")
        if isinstance(verdicts, list):
            for v in verdicts:
                if not isinstance(v, dict):
                    continue
                cid = v.get("candidate_id")
                if not cid:
                    continue
                verdict = str(v.get("verdict") or "").lower()
                if verdict == "pass":
                    self.candidates.set_status(session_id, str(cid), "verified")
                elif verdict == "reject":
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

    # ------------------------------------------------------- integrity layer
    def _run_integrity_pipeline(self, session_id: str, phase_outputs: dict[str, Any]) -> None:
        """Deterministic integrity pipeline, run after Phase 8 and before Phase 9.

        For every non-rejected candidate:
          1. register the candidate's claims from the phase analyses,
          2. run the 12 named deterministic filters,
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
        candidates = [
            c for c in self.candidates.list(session_id, limit=self.rc.max_candidates)
            if c.status != "rejected"
        ]
        existing_niches = [c.niche for c in self.candidates.list(session_id, limit=500)]
        accepted = 0
        downgraded = 0
        rejected = 0
        for cand in candidates:
            self._register_candidate_claims(session_id, cand, phase_outputs)
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

    def _register_candidate_claims(
        self, session_id: str, cand: Any, phase_outputs: dict[str, Any]
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
            )
            existing.add(statement.lower())

        def _mentions(text: str) -> bool:
            return niche[:24] in (text or "").lower()

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
        rows = self.candidates.list(session_id, limit=self.rc.max_candidates)
        return [
            {
                "candidate_id": c.id,
                "niche": c.niche,
                "status": c.status,
                "marketplace": c.marketplace,
                "score": c.score,
                "rationale": c.rationale[:200],
            }
            for c in rows
        ]

    def _progress_signature(self, session_id: str) -> dict[str, int]:
        be_count = self.browser_evidence.count(session_id)
        ev_count = len(self.evidence.list(session_id, limit=1))
        cand_count = len(self.candidates.list(session_id, limit=500))
        return {
            "browser_evidence": be_count,
            "evidence_total": be_count + ev_count,
            "candidates": cand_count,
        }

    def _has_images(self, session_id: str) -> bool:
        from app.uploads import UploadStore

        return bool(UploadStore(self.db, self.config).list(session_id))

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
    def _pause_for_auth(self, session_id: str, exc: AuthPauseRequired) -> None:
        self.sessions.update(session_id, status="paused", error=f"authentication required: {exc}")
        self.agent.transition(session_id, "waiting_recovery", substate="auth_required")
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
        self.errors.record(
            session_id, scope="browser", severity="error", message=str(exc), recoverable=True,
        )
        self.events.append(
            session_id, level="error", actor="browser",
            action="paused_for_browser_failure", detail={"error": str(exc)[:300]},
        )

    def _fail(self, session_id: str, exc: Exception) -> None:
        self.sessions.update(session_id, status="failed", error=str(exc)[:500])
        self.agent.transition(session_id, "failed", substate="runner_error")
        self.errors.record(
            session_id, scope="runner", severity="critical",
            message=f"{type(exc).__name__}: {exc}", recoverable=False,
        )
        self.events.append(
            session_id, level="error", actor="system", action="research_failed",
            detail={"error": str(exc)[:300], "trace": traceback.format_exc()[-800:]},
        )

    def _write_final_report(self, session_id: str, phase_outputs: dict[str, Any]) -> None:
        from app.quality_gates import QualityGateRunner
        from app.report_model import build_report_model, render_markdown
        from app.report_validation import ReportValidator
        from app.research_data import VerificationStore

        # Structured model first — the report is a RENDERING of durable
        # research state, never a free-form string.
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
        )
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
