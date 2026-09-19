"""The autonomous job supervisor: durable lifecycle for long-running research.

A six-hour session must survive container restarts, Space restarts, network
failures, and crashes without starting over. This module is the boss:

- Durable job states (queued/initializing/researching/paused/waiting_auth/
  waiting_manual/recovering/completed/failed/cancelled) persisted in the
  research_jobs table.
- `recover_on_boot`: Restart -> load session state -> recover job -> restore
  browser where possible -> resume from the last safe checkpoint.
- Continuous checkpointing hooks the runner calls as it works.
- Failure isolation: one broken page/marketplace/model call is recorded and
  researched around, never fatal by itself.

The supervisor owns process-level state transitions only; research logic
lives in research_runner. The FastAPI lifespan starts ONE supervisor for
the single shared worker pool (2-CPU target: 1-2 concurrent sessions max).
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from app.config import Config
from app.db import Database
from app.events import EventLog
from app.recovery import CheckpointStore, ErrorStateStore
from app.sessions import SessionStore
from app.timeutil import iso_now

# Durable job lifecycle (the owner-facing autonomy states).
JOB_QUEUED = "queued"
JOB_INITIALIZING = "initializing"
JOB_RESEARCHING = "researching"
JOB_PAUSED = "paused"
JOB_WAITING_AUTH = "waiting_authentication"
JOB_WAITING_MANUAL = "waiting_manual_intervention"
JOB_RECOVERING = "recovering"
JOB_COMPLETED = "completed"
JOB_FAILED = "failed"
JOB_CANCELLED = "cancelled"

JOB_STATUSES = (
    JOB_QUEUED, JOB_INITIALIZING, JOB_RESEARCHING, JOB_PAUSED,
    JOB_WAITING_AUTH, JOB_WAITING_MANUAL, JOB_RECOVERING,
    JOB_COMPLETED, JOB_FAILED, JOB_CANCELLED,
)

# Restart recovery order, executed by recover_on_boot:
#   Restart -> load session state -> recover job -> restore browser
#   where possible -> resume from last safe checkpoint.


@dataclass
class JobState:
    session_id: str
    job_id: str
    status: str
    attempt: int
    max_attempts: int
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "job_id": self.job_id,
            "status": self.status,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "detail": self.detail,
        }


class AutonomySupervisor:
    """Durable research-job supervisor. One per process."""

    def __init__(
        self,
        db: Database,
        config: Config,
        *,
        sessions: SessionStore,
        events: EventLog,
        errors: ErrorStateStore,
        checkpoints: CheckpointStore,
    ) -> None:
        self.db = db
        self.config = config
        self.sessions = sessions
        self.events = events
        self.errors = errors
        self.checkpoints = checkpoints
        self._resume_queue: asyncio.Queue[str] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._stopping = False

    # ------------------------------------------------------------------ states
    def _set_job_status(self, session_id: str, job_id: str, status: str, detail: dict[str, Any] | None = None) -> None:
        if status not in JOB_STATUSES:
            raise ValueError(f"invalid job status {status!r}")
        now = iso_now()
        note = str((detail or {}).get("note", ""))
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE research_jobs SET status = ?, updated_at = ?, "
                "payload = json_set(payload, '$.supervisor_note', ?) WHERE id = ?",
                (status, now, note, job_id),
            )
        self.events.append(
            session_id, level="info", actor="system", action=f"job_{status}",
            detail={"job_id": job_id, **(detail or {})},
        )

    def current_job(self, session_id: str) -> JobState | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM research_jobs WHERE session_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return JobState(
            session_id=session_id,
            job_id=row["id"],
            status=row["status"],
            attempt=row["attempt"],
            max_attempts=row["max_attempts"],
            detail={},
        )

    # ------------------------------------------------------------------ lifecycle
    def mark(self, session_id: str, status: str, *, detail: dict[str, Any] | None = None) -> None:
        """Transition the session's current job to an autonomy state."""
        job = self.current_job(session_id)
        if job is None:
            return
        self._set_job_status(session_id, job.job_id, status, detail)

    # ------------------------------------------------------------------ recovery
    def recover_on_boot(self) -> dict[str, int]:
        """Restart -> load state -> recover job -> restore browser where
        possible -> queue resume from last safe checkpoint.

        Returns counts by action for boot logging. Idempotent.
        """
        counts = {"resumed": 0, "interrupted": 0, "waiting": 0, "failed": 0, "jobs_requeued": 0}
        # 1. Jobs left 'running'-ish by a dead process -> back to queued.
        with self.db.tx() as conn:
            cur = conn.execute(
                "UPDATE research_jobs SET status = 'pending', updated_at = ? "
                "WHERE status IN ('running', 'initializing', 'researching', 'recovering')",
                (iso_now(),),
            )
            counts["jobs_requeued"] = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

        # 2. Sessions in transient states: classify by their last checkpoint.
        for s in self.sessions.list(include_terminal=True):
            if s.status not in ("running", "queued"):
                if s.status in ("paused", "interrupted"):
                    counts["waiting"] += 1
                continue
            paused = self.checkpoints.get(s.id, "paused:auth") or self.checkpoints.get(s.id, "paused:model") \
                or self.checkpoints.get(s.id, "paused:browser")
            if paused is not None:
                # A pause checkpoint exists: the session was mid-wait, keep it
                # paused (owner or supervisor can resume later) — not failed.
                self.sessions.update(s.id, status="paused")
                counts["waiting"] += 1
                self.events.append(
                    s.id, level="warn", actor="system",
                    action="job_recovered_to_waiting", detail={"reason": "pause checkpoint present"},
                )
                continue
            last = self.checkpoints.latest(s.id)
            if last is not None:
                # There IS a safe checkpoint: mark interrupted and queue a
                # resume — a six-hour session must not restart from zero.
                self.sessions.update(s.id, status="interrupted", error=None)
                self.checkpoints.save(s.id, "resume_requested", {"at": iso_now(), "from": last.key})
                counts["resumed"] += 1
            else:
                self.sessions.update(s.id, status="interrupted", error=None)
                counts["interrupted"] += 1
            self.events.append(
                s.id, level="info", actor="system",
                action="job_recovered_after_restart",
                detail={"last_checkpoint": last.key if last else None},
            )
        return counts

    # ------------------------------------------------------------------ auto-resume
    def auto_resume_recovered(self, runners: Any) -> int:
        """Queue interrupted sessions with a resume checkpoint back into the
        runner pool. Called once after boot recovery, after the runner
        registry exists. Research continues without the owner present."""
        resumed = 0
        for s in self.sessions.list(include_terminal=True):
            if s.status != "interrupted":
                continue
            if self.checkpoints.get(s.id, "resume_requested") is None:
                continue
            try:
                self.sessions.bump_resume(s.id)
                self.sessions.update(s.id, status="queued", error=None)
                runners.start(s.id)
                resumed += 1
                self.events.append(
                    s.id, level="info", actor="system",
                    action="session_auto_resumed", detail={},
                )
            except Exception as exc:
                self.errors.record(
                    s.id, scope="autonomy:auto_resume", severity="warning",
                    message=f"auto-resume failed: {exc}", recoverable=True,
                )
        return resumed

    # ------------------------------------------------------------------ heartbeat
    def heartbeat(self, session_id: str, *, phase: str, note: str = "") -> None:
        """Continuous checkpointing: the runner calls this as it works so the
        newest safe checkpoint is always fresh."""
        self.checkpoints.save(session_id, "heartbeat", {
            "phase": phase, "note": note[:200], "at": iso_now(),
        })
