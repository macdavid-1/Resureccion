"""The model's own long-running memory for one research run.

A research run lasts hours and spans nine phases. The structured record (phase
outputs, candidates, captures, claims) holds the things the backend can store;
`notes` holds short annotations; neither is the model's own account of what it
is doing. This module is that account:

- **The model writes it**, mid-research, with the `remember` action — findings,
  decisions and why, what it ruled out, open questions, where it means to go
  next, corrections to an earlier belief.
- **It is handed back in full on every step**, oldest first, unparsed and
  uncondensed. That is what makes it memory rather than a log: the model
  re-reads its own reasoning after context churn, a pause, a restart, or a
  follow-up run that inherited it.
- **It is never summarized by the backend.** No digests, no field extraction,
  no caps on how much is carried. The only thing the backend owns is the order
  and the provenance.

Because a follow-up run inherits the previous run's entries (marked with their
origin session), an investigation's memory grows across runs instead of being
replaced by a brief about them.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from app.db import Database
from app.timeutil import iso_now

# The action the model uses to write a memory entry. Available in every phase
# (see app.methodology): memory belongs to the run, not to a phase.
REMEMBER = "remember"
MEMORY_ACTIONS: tuple[str, ...] = (REMEMBER,)


@dataclass
class MemoryEntry:
    id: int
    session_id: str
    created_at: str
    phase: str
    kind: str
    origin: str
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "phase": self.phase,
            "kind": self.kind,
            "origin": self.origin,
            "text": self.text,
        }


class ResearchMemoryStore:
    """Durable, append-only, uncapped memory journal for a research session."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def append(
        self,
        session_id: str,
        *,
        text: str,
        phase: str = "",
        kind: str = "memory",
        origin: str = "",
    ) -> MemoryEntry:
        """Store one entry verbatim.

        Nothing is trimmed: a memory entry is the model's own words and it is
        re-read by the model itself. An empty entry is refused (it carries
        nothing to remember), everything else is stored as written.
        """
        body = str(text or "")
        if not body.strip():
            raise ValueError("memory entry text must not be empty")
        now = iso_now()
        with self.db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO research_memory "
                "(session_id, created_at, phase, kind, origin, text) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    now,
                    str(phase or "")[:80],
                    str(kind or "memory")[:60],
                    str(origin or "")[:64],
                    body,
                ),
            )
            entry_id = int(cur.lastrowid or 0)
        return MemoryEntry(
            id=entry_id, session_id=session_id, created_at=now,
            phase=str(phase or "")[:80], kind=str(kind or "memory")[:60],
            origin=str(origin or "")[:64], text=body,
        )

    def list(self, session_id: str) -> list[MemoryEntry]:
        """EVERY entry for the session, oldest first — no limit, no paging."""
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM research_memory WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            ).fetchall()
        return [self._row(r) for r in rows]

    def count(self, session_id: str) -> int:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM research_memory WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return int(row["n"]) if row else 0

    def chars(self, session_id: str) -> int:
        """Total characters held for the session (owner-facing size only)."""
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(LENGTH(text)), 0) AS n FROM research_memory "
                "WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return int(row["n"]) if row else 0

    def _row(self, row: sqlite3.Row) -> MemoryEntry:
        return MemoryEntry(
            id=int(row["id"]),
            session_id=str(row["session_id"]),
            created_at=str(row["created_at"]),
            phase=str(row["phase"]),
            kind=str(row["kind"]),
            origin=str(row["origin"]),
            text=str(row["text"]),
        )


def render_memory(entries: list[Any]) -> str:
    """The prompt section: every entry, in order, verbatim.

    Deliberately not a digest: the model wrote these lines for itself, and it
    is the accumulation (not a summary of it) that keeps its reasoning intact
    after a phase change, a restart or a new run. Inherited entries say where
    they came from so the model can tell its own earlier self from this run.
    """
    lines: list[str] = []
    for e in entries:
        phase = getattr(e, "phase", "") or ""
        kind = getattr(e, "kind", "") or "memory"
        origin = getattr(e, "origin", "") or ""
        stamp = str(getattr(e, "created_at", "") or "")
        head = f"[{kind}"
        if phase:
            head += f" · {phase}"
        if stamp:
            head += f" · {stamp}"
        if origin:
            head += f" · inherited from session {origin[:12]}"
        head += "]"
        lines.append(f"- {head}\n  " + str(getattr(e, "text", "") or ""))
    return "\n".join(lines)
