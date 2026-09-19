"""Research event / action log.

Append-only log of everything the agent does, keyed by session. Used by the
dashboard for live tailing and by post-mortem analysis after a failure.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from app.db import Database
from app.timeutil import iso_now

LEVELS = ("debug", "info", "warn", "error")
ACTORS = ("system", "agent", "browser", "owner", "model")


@dataclass
class Event:
    id: int
    session_id: str
    created_at: str
    level: str
    actor: str
    action: str
    detail: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "level": self.level,
            "actor": self.actor,
            "action": self.action,
            "detail": self.detail,
        }


class EventLog:
    def __init__(self, db: Database) -> None:
        self.db = db

    def append(
        self,
        session_id: str,
        *,
        level: str = "info",
        actor: str = "system",
        action: str,
        detail: dict[str, Any] | None = None,
    ) -> Event:
        if level not in LEVELS:
            raise ValueError(f"invalid event level {level!r}")
        if actor not in ACTORS:
            raise ValueError(f"invalid event actor {actor!r}")
        with self.db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO events (session_id, created_at, level, actor, action, detail) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, iso_now(), level, actor, action, json.dumps(detail or {})),
            )
            event_id = cur.lastrowid
        assert event_id is not None
        return Event(
            id=event_id,
            session_id=session_id,
            created_at=iso_now(),
            level=level,
            actor=actor,
            action=action,
            detail=detail or {},
        )

    def tail(
        self, session_id: str, *, after_id: int = 0, limit: int = 200
    ) -> list[Event]:
        limit = max(1, min(limit, 1000))
        with self.db.read() as conn:
            rows = conn.execute(
                """
                SELECT * FROM events
                WHERE session_id = ? AND id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (session_id, after_id, limit),
            ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def latest_errors(self, session_id: str, limit: int = 50) -> list[Event]:
        with self.db.read() as conn:
            rows = conn.execute(
                """
                SELECT * FROM events
                WHERE session_id = ? AND level = 'error'
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_id, max(1, min(limit, 200))),
            ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def _row_to_event(self, row: sqlite3.Row) -> Event:
        return Event(
            id=row["id"],
            session_id=row["session_id"],
            created_at=row["created_at"],
            level=row["level"],
            actor=row["actor"],
            action=row["action"],
            detail=json.loads(row["detail"]),
        )
