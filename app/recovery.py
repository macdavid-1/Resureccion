"""Recovery / checkpoint state.

Checkpoints are arbitrary JSON payloads stored per (session, key). The job
orchestrator writes one before each major step and after each completed step;
on resume, the agent replays from the newest checkpoint instead of starting
over. Also provides the boot-time recovery scan that marks sessions left in
transient states by a crash/restart as `interrupted`.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from app.db import Database
from app.sessions import SessionStore
from app.timeutil import iso_now

TRANSIENT_STATUSES = ("queued", "running")


@dataclass
class Checkpoint:
    session_id: str
    key: str
    created_at: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "key": self.key,
            "created_at": self.created_at,
            "payload": self.payload,
        }


class CheckpointStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, session_id: str, key: str, payload: dict[str, Any]) -> Checkpoint:
        if not key:
            raise ValueError("checkpoint key must be non-empty")
        now = iso_now()
        with self.db.tx() as conn:
            conn.execute(
                """
                INSERT INTO checkpoints (session_id, key, created_at, payload)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id, key) DO UPDATE SET
                    created_at = excluded.created_at,
                    payload = excluded.payload
                """,
                (session_id, key, now, json.dumps(payload)),
            )
        return Checkpoint(session_id=session_id, key=key, created_at=now, payload=payload)

    def get(self, session_id: str, key: str) -> Checkpoint | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM checkpoints WHERE session_id = ? AND key = ?",
                (session_id, key),
            ).fetchone()
        return self._row(row) if row else None

    def get_safe(self, session_id: str, key: str) -> Checkpoint | None:
        """Like get(), but a corrupted payload degrades to None instead of
        crashing boot recovery. Storage-level atomic writes make corruption
        unlikely; this is the last line of defense so one bad checkpoint
        can never take down restart recovery."""
        try:
            return self.get(session_id, key)
        except Exception:
            return None

    def latest(self, session_id: str) -> Checkpoint | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM checkpoints WHERE session_id = ? ORDER BY created_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return self._row(row) if row else None

    def latest_safe(self, session_id: str) -> Checkpoint | None:
        try:
            return self.latest(session_id)
        except Exception:
            return None

    def list(self, session_id: str) -> list[Checkpoint]:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM checkpoints WHERE session_id = ? ORDER BY created_at DESC",
                (session_id,),
            ).fetchall()
        out: list[Checkpoint] = []
        for r in rows:
            try:
                out.append(self._row(r))
            except Exception:
                continue  # skip corrupted rows; never poison the whole list
        return out

    def delete(self, session_id: str, key: str) -> None:
        with self.db.tx() as conn:
            conn.execute(
                "DELETE FROM checkpoints WHERE session_id = ? AND key = ?",
                (session_id, key),
            )

    def _row(self, row: sqlite3.Row) -> Checkpoint:
        try:
            payload = json.loads(row["payload"])
        except Exception:
            # Corrupted payload (e.g. partial write outside atomic path):
            # keep the row's existence visible with an empty payload rather
            # than raising — recovery logic decides what to do.
            payload = {}
        if not isinstance(payload, dict):
            payload = {"_value": payload}
        return Checkpoint(
            session_id=row["session_id"],
            key=row["key"],
            created_at=row["created_at"],
            payload=payload,
        )


class ErrorStateStore:
    """Durable record of failures, recoverable or not."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def record(
        self,
        session_id: str,
        *,
        scope: str,
        severity: str,
        message: str,
        recoverable: bool = True,
    ) -> None:
        if severity not in ("info", "warning", "error", "critical"):
            raise ValueError(f"invalid severity {severity!r}")
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO error_states (session_id, created_at, scope, severity, message, recoverable) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, iso_now(), scope, severity, message, 1 if recoverable else 0),
            )

    def resolve(self, session_id: str, error_id: int) -> None:
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE error_states SET resolved_at = ? WHERE session_id = ? AND id = ?",
                (iso_now(), session_id, error_id),
            )

    def unresolved(self, session_id: str) -> list[dict[str, Any]]:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM error_states WHERE session_id = ? AND resolved_at IS NULL ORDER BY id DESC",
                (session_id,),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "session_id": r["session_id"],
                "created_at": r["created_at"],
                "scope": r["scope"],
                "severity": r["severity"],
                "message": r["message"],
                "recoverable": bool(r["recoverable"]),
            }
            for r in rows
        ]


class RecoveryManager:
    """Boot-time crash recovery for research sessions.

    Any session left in a transient status (queued/running) by a previous
    process is marked `interrupted` with a reason, and its agent state is
    moved to `waiting_recovery`. The orchestrator can then resume it.
    """

    def __init__(self, db: Database, sessions: SessionStore, checkpoints: CheckpointStore, errors: ErrorStateStore) -> None:
        self.db = db
        self.sessions = sessions
        self.checkpoints = checkpoints
        self.errors = errors

    def recover_all(self) -> list[str]:
        recovered: list[str] = []
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT id FROM research_sessions WHERE status IN ('queued','running')"
            ).fetchall()
        for row in rows:
            session_id = row["id"]
            reason = "recovered after application restart"
            self.sessions.mark_interrupted(session_id, reason)
            self.errors.record(
                session_id,
                scope="recovery",
                severity="warning",
                message=reason,
                recoverable=True,
            )
            recovered.append(session_id)
        return recovered
