"""Research job orchestrator.

Jobs are the durable unit of research work. Every job row survives restarts;
the orchestrator reconciles job state with session state at boot and when the
owner requests a resume. Attempt limits, heartbeats, and per-job errors are
recorded durably so a failed task never vanishes silently.

This module defines the orchestration *contract*; the actual browser-driven
research executor plugs in later behind `JobExecutor`.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from app.config import Config
from app.db import Database
from app.events import EventLog
from app.recovery import ErrorStateStore
from app.sessions import SessionStore
from app.timeutil import iso_now

JOB_STATUSES = ("pending", "running", "succeeded", "failed", "cancelled")


class JobError(Exception):
    pass


@dataclass
class Job:
    id: str
    session_id: str
    kind: str
    status: str
    attempt: int
    max_attempts: int
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None
    error: str | None
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "kind": self.kind,
            "status": self.status,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "payload": self.payload,
        }


class JobExecutor(Protocol):
    """The future research executor implements this interface."""

    def execute(self, job: Job) -> None:
        ...


class JobOrchestrator:
    def __init__(
        self,
        db: Database,
        config: Config,
        sessions: SessionStore,
        events: EventLog,
        errors: ErrorStateStore,
    ) -> None:
        self.db = db
        self.config = config
        self.sessions = sessions
        self.events = events
        self.errors = errors

    # ------------------------------------------------------------------ create
    def create_job(
        self,
        session_id: str,
        *,
        kind: str,
        payload: dict[str, Any] | None = None,
        max_attempts: int = 3,
    ) -> Job:
        self.sessions.require(session_id)
        if not kind or not kind.strip():
            raise JobError("job kind must be non-empty")
        job_id = uuid.uuid4().hex
        now = iso_now()
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO research_jobs (id, session_id, kind, status, attempt, max_attempts, created_at, updated_at, payload) VALUES (?, ?, ?, 'pending', 0, ?, ?, ?, ?)",
                (job_id, session_id, kind, max_attempts, now, now, json.dumps(payload or {})),
            )
        self.events.append(
            session_id,
            level="info",
            actor="system",
            action="job_created",
            detail={"job_id": job_id, "kind": kind},
        )
        return self.get(session_id, job_id)  # type: ignore[return-value]

    # -------------------------------------------------------------- lifecycle
    def start_job(self, session_id: str, job_id: str) -> Job:
        job = self.get(session_id, job_id)
        if job is None:
            raise JobError(f"job {job_id} not found")
        if job.status not in ("pending", "failed"):
            raise JobError(f"cannot start job in status {job.status!r}")
        if job.attempt >= job.max_attempts:
            raise JobError(f"job {job_id} exhausted attempts ({job.attempt}/{job.max_attempts})")
        now = iso_now()
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE research_jobs SET status = 'running', attempt = attempt + 1, started_at = COALESCE(started_at, ?), updated_at = ? WHERE id = ?",
                (now, now, job_id),
            )
        return self.get(session_id, job_id)  # type: ignore[return-value]

    def complete_job(self, session_id: str, job_id: str, *, error: str | None = None) -> Job:
        job = self.get(session_id, job_id)
        if job is None:
            raise JobError(f"job {job_id} not found")
        status = "failed" if error else "succeeded"
        now = iso_now()
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE research_jobs SET status = ?, finished_at = ?, updated_at = ?, error = ? WHERE id = ?",
                (status, now, now, error, job_id),
            )
        if error:
            self.errors.record(
                session_id,
                scope=f"job:{job.kind}",
                severity="error",
                message=error,
                recoverable=job.attempt < job.max_attempts,
            )
            self.events.append(
                session_id,
                level="error",
                actor="system",
                action="job_failed",
                detail={"job_id": job_id, "kind": job.kind, "error": error},
            )
        else:
            self.events.append(
                session_id,
                level="info",
                actor="system",
                action="job_succeeded",
                detail={"job_id": job_id, "kind": job.kind},
            )
        return self.get(session_id, job_id)  # type: ignore[return-value]

    def cancel_job(self, session_id: str, job_id: str) -> Job:
        job = self.get(session_id, job_id)
        if job is None:
            raise JobError(f"job {job_id} not found")
        if job.status in ("succeeded", "cancelled"):
            return job
        now = iso_now()
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE research_jobs SET status = 'cancelled', finished_at = ?, updated_at = ? WHERE id = ?",
                (now, now, job_id),
            )
        return self.get(session_id, job_id)  # type: ignore[return-value]

    # ------------------------------------------------------------------ query
    def get(self, session_id: str, job_id: str) -> Job | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM research_jobs WHERE session_id = ? AND id = ?",
                (session_id, job_id),
            ).fetchone()
        return self._row(row) if row else None

    def list_jobs(self, session_id: str, *, status: str | None = None) -> list[Job]:
        if status is not None and status not in JOB_STATUSES:
            raise JobError(f"invalid job status {status!r}")
        q = "SELECT * FROM research_jobs WHERE session_id = ?"
        params: list[Any] = [session_id]
        if status is not None:
            q += " AND status = ?"
            params.append(status)
        q += " ORDER BY created_at ASC"
        with self.db.read() as conn:
            rows = conn.execute(q, params).fetchall()
        return [self._row(r) for r in rows]

    def active_jobs(self, session_id: str) -> list[Job]:
        return [j for j in self.list_jobs(session_id) if j.status in ("pending", "running")]

    # --------------------------------------------------------------- recovery
    def reconcile_on_boot(self) -> dict[str, int]:
        """After a restart, requeue interrupted work and reset stuck rows.

        Jobs left `running` by a dead process are moved back to `pending` so
        they can be retried (within their attempt budget). Returns counts.
        """
        with self.db.tx() as conn:
            cur = conn.execute(
                """
                UPDATE research_jobs SET status = 'pending', updated_at = ?
                WHERE status = 'running'
                """,
                (iso_now(),),
            )
            reset_running = cur.rowcount
        return {"requeued_running_jobs": reset_running}

    def _row(self, row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            session_id=row["session_id"],
            kind=row["kind"],
            status=row["status"],
            attempt=row["attempt"],
            max_attempts=row["max_attempts"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            error=row["error"],
            payload=json.loads(row["payload"]),
        )
