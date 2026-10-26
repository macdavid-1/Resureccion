"""Owner-visible chain of thought: one durable row per model call.

Resurrección's stance on chain-of-thought is that it is the MODEL's private
workspace: the operational trace (`app.trace`) carries the sanctioned one-line
"what and why", and raw reasoning never enters events, evidence, recordings,
exports or reports. This module is the single deliberate exception, and only
for the one authorized human: the owner opens the session and watches the
reasoning grow while the call streams.

Two halves:

- **durable** — a `thoughts` row per call, written at call start and
  finalized at call end, so an app restart, a Space restart or a crash never
  silently loses what the model had produced. Detail is flushed at most once
  per second while streaming (bounded writes) and always on finish.
- **live** — the in-memory buffer of the call that is streaming right now.
  The SSE snapshot overlays it, so the expanded row updates as tokens arrive.
  The buffer is an optimization only: `get()` reads the DB and the DB is
  never more than ~1s behind.

Status lifecycle: ``streaming`` -> ``done`` | ``failed``, plus
``interrupted`` stamped by :meth:`ThoughtStore.recover_stale` at boot for
rows whose process died mid-call (the row survives, the label and whatever
detail was flushed survive with it).
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from app.db import Database
from app.timeutil import iso_now

STATUS_STREAMING = "streaming"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_INTERRUPTED = "interrupted"
STATUSES = (STATUS_STREAMING, STATUS_DONE, STATUS_FAILED, STATUS_INTERRUPTED)

# Reasoning is one call's worth of thinking, not a log: a hard cap keeps a
# pathological run from growing a single row without bound.
MAX_DETAIL_CHARS = 160_000
# At most one durable write per second while streaming; `finish()` always
# writes the final text regardless of this window.
FLUSH_SECONDS = 1.0
# How many summaries the live snapshot carries (the feed is append-mostly).
RECENT_LIMIT = 40


@dataclass
class Thought:
    id: int
    session_id: str
    created_at: str
    updated_at: str
    label: str
    status: str
    chars: int
    detail: str

    def summary_dict(self) -> dict:
        """List/snapshot shape — deliberately WITHOUT the reasoning text."""
        return {
            "id": self.id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "label": self.label,
            "status": self.status,
            "chars": self.chars,
        }

    def to_dict(self) -> dict:
        """Full shape for the detail endpoint and the live overlay."""
        return {**self.summary_dict(), "session_id": self.session_id, "detail": self.detail}


class ThoughtStore:
    """Durable + live chain-of-thought rows for every session."""

    def __init__(self, db: Database) -> None:
        self.db = db
        # thought_id -> buffer for calls streaming in THIS process. Never the
        # only copy of anything: every mutation is flushed to SQLite.
        self._live: dict[int, dict] = {}

    # ------------------------------------------------------------- writing
    def begin(self, session_id: str, *, label: str = "Thinking") -> int:
        now = iso_now()
        with self.db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO thoughts (session_id, created_at, updated_at, label, status, chars, detail) "
                "VALUES (?, ?, ?, ?, ?, 0, '')",
                (session_id, now, now, _clean_label(label), STATUS_STREAMING),
            )
            thought_id = int(cur.lastrowid)
        self._live[thought_id] = {"session_id": session_id, "detail": "", "flushed": time.monotonic()}
        return thought_id

    def append(self, thought_id: int, delta: str) -> None:
        """Accumulate a reasoning delta; flush at most once per second."""
        buf = self._live.get(thought_id)
        if buf is None or not delta:
            return
        room = MAX_DETAIL_CHARS - len(buf["detail"])
        if room <= 0:
            return
        buf["detail"] += delta[:room]
        if time.monotonic() - buf["flushed"] >= FLUSH_SECONDS:
            self._write(thought_id, buf["detail"])

    def restart(self, thought_id: int) -> None:
        """Drop streamed detail — the caller is re-asking the provider, so
        what the discarded attempt produced must not survive as if it did."""
        buf = self._live.get(thought_id)
        if buf is None:
            return
        buf["detail"] = ""
        buf["flushed"] = time.monotonic()
        self._write(thought_id, "")

    def flush(self, thought_id: int) -> None:
        """Persist what has arrived so far, outside the 1s throttle window.
        `finish()` always flushes; this is for 'the process may die now'."""
        buf = self._live.get(thought_id)
        if buf is None:
            return
        self._write(thought_id, buf["detail"])

    def finish(self, thought_id: int, *, status: str = STATUS_DONE, label: str | None = None) -> None:
        if status not in STATUSES:
            raise ValueError(f"invalid thought status {status!r}")
        buf = self._live.pop(thought_id, None)
        detail = buf["detail"] if buf is not None else None
        now = iso_now()
        with self.db.tx() as conn:
            if detail is not None:
                conn.execute(
                    "UPDATE thoughts SET detail = ?, chars = ?, updated_at = ?, status = ?, "
                    "label = COALESCE(?, label) WHERE id = ?",
                    (detail, len(detail), now, status, _clean_label(label) if label else None, thought_id),
                )
            else:
                conn.execute(
                    "UPDATE thoughts SET updated_at = ?, status = ?, "
                    "label = COALESCE(?, label) WHERE id = ?",
                    (now, status, _clean_label(label) if label else None, thought_id),
                )

    def summarize(self, thought_id: int, label: str) -> None:
        """Upgrade the one-line summary (the model's own activity_note)."""
        text = _clean_label(label)
        if not text:
            return
        with self.db.tx() as conn:
            conn.execute("UPDATE thoughts SET label = ?, updated_at = ? WHERE id = ?",
                         (text, iso_now(), thought_id))

    def recover_stale(self) -> int:
        """At boot: rows still marked streaming belonged to a process that is
        gone. Keep the row and its detail, but stop claiming it is live."""
        with self.db.tx() as conn:
            cur = conn.execute(
                "UPDATE thoughts SET status = ?, updated_at = ? WHERE status = ?",
                (STATUS_INTERRUPTED, iso_now(), STATUS_STREAMING),
            )
            return int(cur.rowcount or 0)

    # -------------------------------------------------------------- reading
    def recent(self, session_id: str, *, limit: int = RECENT_LIMIT) -> list[dict]:
        """Summaries, oldest first, WITHOUT reasoning text (bounded payload)."""
        limit = max(1, min(int(limit), 200))
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT id, session_id, created_at, updated_at, label, status, chars, detail "
                "FROM thoughts WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        out = []
        for row in reversed(list(rows)):
            t = _row(row)
            buf = self._live.get(t.id)
            if buf is not None:  # DB lags up to a second while streaming
                t.chars = len(buf["detail"])
            out.append(t.summary_dict())
        return out

    def live(self, session_id: str) -> dict | None:
        """The call that is thinking right now, WITH its current detail.

        Reads the in-memory buffer when this process owns the call and falls
        back to the row itself (e.g. detail flushed just before a restart).
        """
        newest: tuple[int, dict] | None = None
        for thought_id, buf in self._live.items():
            if buf["session_id"] == session_id and (newest is None or thought_id > newest[0]):
                newest = (thought_id, buf)
        if newest is not None:
            t = self.get(newest[0])
            if t is not None:
                return t.to_dict()
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM thoughts WHERE session_id = ? AND status = ? ORDER BY id DESC LIMIT 1",
                (session_id, STATUS_STREAMING),
            ).fetchone()
        return _row(row).to_dict() if row else None

    def get(self, thought_id: int) -> Thought | None:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM thoughts WHERE id = ?", (thought_id,)).fetchone()
        if row is None:
            return None
        t = _row(row)
        buf = self._live.get(thought_id)
        if buf is not None:
            t.detail = buf["detail"]
            t.chars = len(buf["detail"])
        return t

    def has_detail(self, thought_id: int) -> bool:
        """True when the live buffer already holds reasoning for this call
        (guards against capturing the same text twice)."""
        buf = self._live.get(thought_id)
        return bool(buf and buf["detail"])

    # ------------------------------------------------------------- internal
    def _write(self, thought_id: int, detail: str) -> None:
        buf = self._live.get(thought_id)
        if buf is not None:
            buf["flushed"] = time.monotonic()
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE thoughts SET detail = ?, chars = ?, updated_at = ? WHERE id = ?",
                (detail, len(detail), iso_now(), thought_id),
            )


def _clean_label(label: str | None) -> str:
    return " ".join(str(label or "").split())[:200]


def _row(row: sqlite3.Row) -> Thought:
    return Thought(
        id=int(row["id"]),
        session_id=str(row["session_id"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        label=str(row["label"]),
        status=str(row["status"]),
        chars=int(row["chars"]),
        detail=str(row["detail"]),
    )
