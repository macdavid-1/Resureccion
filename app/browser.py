"""Browser session registry.

Research uses a real browser (Playwright-managed) per research session. The
browser process itself comes later; this module is the durable registry that
tracks each session's browser state — profile, launch config, last-known
health — so a crashed browser can be detected and relaunched without losing
the research session's identity.

KDSpy Pro / marketplace account metadata is configuration-only here: the
registry stores *metadata* (which profile to use), never credentials.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from app.db import Database
from app.sessions import SessionStore
from app.timeutil import iso_now

BROWSER_STATUSES = ("none", "launching", "ready", "busy", "crashed", "closed")


class BrowserRegistryError(Exception):
    pass


@dataclass
class BrowserSession:
    session_id: str
    status: str
    profile: str
    user_agent: str
    viewport_width: int
    viewport_height: int
    launch_config: dict[str, Any]
    last_health_at: str | None
    crash_count: int
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "profile": self.profile,
            "user_agent": self.user_agent,
            "viewport": {"width": self.viewport_width, "height": self.viewport_height},
            "launch_config": self.launch_config,
            "last_health_at": self.last_health_at,
            "crash_count": self.crash_count,
            "updated_at": self.updated_at,
        }


class BrowserRegistry:
    def __init__(self, db: Database, sessions: SessionStore) -> None:
        self.db = db
        self.sessions = sessions

    def ensure(self, session_id: str, *, profile: str = "default") -> BrowserSession:
        self.sessions.require(session_id)
        now = iso_now()
        with self.db.tx() as conn:
            conn.execute(
                """
                INSERT INTO browser_sessions (session_id, status, profile, launch_config, updated_at)
                VALUES (?, 'none', ?, '{}', ?)
                ON CONFLICT(session_id) DO NOTHING
                """,
                (session_id, profile, now),
            )
        return self.get(session_id)  # type: ignore[return-value]

    def get(self, session_id: str) -> BrowserSession | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM browser_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return self._row(row) if row else None

    def require(self, session_id: str) -> BrowserSession:
        b = self.get(session_id)
        if b is None:
            raise BrowserRegistryError(f"browser session for {session_id} not registered")
        return b

    def set_status(self, session_id: str, status: str) -> BrowserSession:
        if status not in BROWSER_STATUSES:
            raise BrowserRegistryError(f"invalid browser status {status!r}")
        self.require(session_id)
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE browser_sessions SET status = ?, updated_at = ? WHERE session_id = ?",
                (status, iso_now(), session_id),
            )
        return self.get(session_id)  # type: ignore[return-value]

    def record_crash(self, session_id: str, reason: str) -> BrowserSession:
        b = self.require(session_id)
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE browser_sessions SET status = 'crashed', crash_count = crash_count + 1, updated_at = ? WHERE session_id = ?",
                (iso_now(), session_id),
            )
        return self.get(session_id)  # type: ignore[return-value]

    def record_health(self, session_id: str) -> BrowserSession:
        self.require(session_id)
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE browser_sessions SET status = CASE WHEN status = 'launching' THEN 'ready' ELSE status END, last_health_at = ?, updated_at = ? WHERE session_id = ?",
                (iso_now(), iso_now(), session_id),
            )
        return self.get(session_id)  # type: ignore[return-value]

    def set_launch_config(self, session_id: str, config: dict[str, Any]) -> BrowserSession:
        self.require(session_id)
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE browser_sessions SET launch_config = ?, updated_at = ? WHERE session_id = ?",
                (json.dumps(config), iso_now(), session_id),
            )
        return self.get(session_id)  # type: ignore[return-value]

    def mark_closed(self, session_id: str) -> BrowserSession:
        return self.set_status(session_id, "closed")

    def crashed_sessions(self) -> list[str]:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT session_id FROM browser_sessions WHERE status = 'crashed'"
            ).fetchall()
        return [r["session_id"] for r in rows]

    def _row(self, row: sqlite3.Row) -> BrowserSession:
        return BrowserSession(
            session_id=row["session_id"],
            status=row["status"],
            profile=row["profile"],
            user_agent=row["user_agent"],
            viewport_width=row["viewport_width"],
            viewport_height=row["viewport_height"],
            launch_config=json.loads(row["launch_config"]),
            last_health_at=row["last_health_at"],
            crash_count=row["crash_count"],
            updated_at=row["updated_at"],
        )
