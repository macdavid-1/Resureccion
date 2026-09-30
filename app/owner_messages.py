"""Owner→agent mid-research messaging.

The owner watches the live research process and can type guidance at any
moment. The message must reach the model WITHOUT interrupting the run and
WITHOUT any line of context being lost. Design:

- `post()` appends a durable row (WAL + synchronous=FULL, like everything
  else) — sending never touches the live runner task.
- `consume_pending()` atomically selects the undelivered rows and marks them
  delivered in the SAME transaction, so every message is handed to the model
  exactly once, even across crashes/restarts of the runner.
- Delivery is not deletion: `journal()` returns every message ever sent, and
  the runner re-includes the FULL journal in every subsequent model prompt.
  The owner's words therefore remain part of the agent's durable context for
  the rest of the session — a resumed session sees them too.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from app.db import Database
from app.timeutil import iso_now


@dataclass
class OwnerMessage:
    id: int
    session_id: str
    created_at: str
    text: str
    delivered_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "text": self.text,
            "delivered": self.delivered_at is not None,
        }


class OwnerMessageStore:
    """Durable owner→agent message channel (SQLite-backed)."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ write
    def post(self, session_id: str, text: str) -> OwnerMessage:
        """Append an owner message. Returns the stored row (always marked
        undelivered at creation — delivery is the runner's job)."""
        text = " ".join(str(text).split())
        if not text:
            raise ValueError("message text must not be empty")
        with self.db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO owner_messages (session_id, created_at, text, delivered_at) "
                "VALUES (?, ?, ?, NULL)",
                (session_id, iso_now(), text),
            )
            mid = int(cur.lastrowid or 0)
        return OwnerMessage(id=mid, session_id=session_id, created_at=iso_now(), text=text, delivered_at=None)

    def mark_delivered(self, ids: list[int]) -> int:
        """Flag rows as delivered (idempotent; used by tests/backfills)."""
        if not ids:
            return 0
        now = iso_now()
        with self.db.tx() as conn:
            cur = conn.execute(
                f"UPDATE owner_messages SET delivered_at = ? "
                f"WHERE delivered_at IS NULL AND id IN ({','.join('?' * len(ids))})",
                (now, *ids),
            )
            return int(cur.rowcount or 0)

    # ------------------------------------------------------------------- read
    def consume_pending(self, session_id: str) -> list[OwnerMessage]:
        """Atomically claim undelivered messages for this session.

        The SELECT and the delivery marker commit together, so a crash
        between fetch and inject either re-delivers (no loss) or never
        needed to — never silently drops a message.
        """
        now = iso_now()
        with self.db.tx() as conn:
            rows = conn.execute(
                "SELECT * FROM owner_messages WHERE session_id = ? AND delivered_at IS NULL "
                "ORDER BY id ASC",
                (session_id,),
            ).fetchall()
            if not rows:
                return []
            conn.execute(
                f"UPDATE owner_messages SET delivered_at = ? "
                f"WHERE session_id = ? AND delivered_at IS NULL",
                (now, session_id),
            )
        # Rows were selected pre-UPDATE; stamp the delivery marker the same
        # transaction just committed so callers see the final state.
        return [
            OwnerMessage(
                id=r["id"], session_id=r["session_id"], created_at=r["created_at"],
                text=r["text"], delivered_at=now,
            )
            for r in rows
        ]

    def journal(self, session_id: str, limit: int = 200) -> list[OwnerMessage]:
        """Every owner message for the session, oldest first — the durable
        guidance context re-included in each prompt."""
        limit = max(1, min(limit, 500))
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM owner_messages WHERE session_id = ? ORDER BY id ASC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [self._row(r) for r in rows]

    def count_pending(self, session_id: str) -> int:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM owner_messages WHERE session_id = ? AND delivered_at IS NULL",
                (session_id,),
            ).fetchone()
        return int(row["n"]) if row else 0

    def _row(self, row: sqlite3.Row) -> OwnerMessage:
        return OwnerMessage(
            id=row["id"],
            session_id=row["session_id"],
            created_at=row["created_at"],
            text=row["text"],
            delivered_at=row["delivered_at"],
        )
