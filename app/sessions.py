"""Research sessions.

A research session is the root of isolation: every other entity (jobs, agent
state, observations, evidence, candidates, verifications, opportunities,
reports, artifacts, uploads, events, errors, checkpoints) carries a
`session_id` FK and all storage paths live under a per-session directory.

Modes:
- `prompt`    : owner gave a detailed research prompt
- `keywords`  : owner gave keywords / rough concepts
- `auto`      : no topic at all — agent autonomously discovers niches

Inputs: text and/or images, with optional marketplace restrictions.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from app.config import Config
from app.db import Database
from app.timeutil import iso_now

STATUSES = (
    "draft",          # created, not yet started
    "queued",         # accepted by orchestrator, waiting for a worker slot
    "running",        # actively researching
    "interrupted",    # container/app restart or crash — resumable
    "paused",         # owner-requested pause
    "completed",      # finished normally with a report
    "failed",         # unrecoverable failure
    "cancelled",      # owner cancelled
)
PHASES = (
    "initializing",
    "discovery",
    "exploration",
    "verification",
    "synthesis",
    "reporting",
    "done",
)

# Session modes and expected inputs
MODE_PROMPT = "prompt"
MODE_KEYWORDS = "keywords"
MODE_AUTO = "auto"


@dataclass
class Session:
    id: str
    name: str
    status: str
    mode: str
    prompt: str
    objective: str
    marketplaces: list[str]
    phase: str
    progress: float
    created_at: str
    updated_at: str
    last_activity_at: str
    elapsed_seconds: float
    error: str | None
    resume_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "mode": self.mode,
            "prompt": self.prompt,
            "objective": self.objective,
            "marketplaces": self.marketplaces,
            "phase": self.phase,
            "progress": self.progress,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_activity_at": self.last_activity_at,
            "elapsed_seconds": self.elapsed_seconds,
            "error": self.error,
            "resume_count": self.resume_count,
        }


def generate_session_name(objective: str, mode: str) -> str:
    """Human-readable auto name. AI-generated names will replace this later."""
    stamp = utc_now_compact()
    if mode == MODE_AUTO:
        base = "Autonomous Market Sweep"
    elif objective.strip():
        words = " ".join(objective.strip().split())[:60]
        base = words
    else:
        base = "Research Session"
    return f"{base} — {stamp}"


def utc_now_compact() -> str:
    from app.timeutil import utcnow

    return utcnow().strftime("%Y-%m-%d %H:%M")


class SessionError(Exception):
    pass


class SessionStore:
    def __init__(self, db: Database, config: Config) -> None:
        self.db = db
        self.config = config

    # ------------------------------------------------------------ create/list
    def create(
        self,
        *,
        mode: str,
        prompt: str = "",
        objective: str = "",
        marketplaces: list[str] | None = None,
        has_images: bool = False,
        name: str | None = None,
    ) -> Session:
        if mode not in (MODE_PROMPT, MODE_KEYWORDS, MODE_AUTO):
            raise SessionError(f"invalid mode {mode!r}")
        if mode == MODE_PROMPT and not prompt.strip():
            raise SessionError("mode 'prompt' requires non-empty prompt text")
        if mode == MODE_KEYWORDS and not (prompt.strip() or has_images):
            raise SessionError("mode 'keywords' requires text or images")
        if mode == MODE_AUTO and prompt.strip():
            raise SessionError("mode 'auto' must not include a prompt; use 'keywords' or 'prompt'")

        with self.db.tx() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM research_sessions WHERE status IN ('draft','queued','running','interrupted','paused')"
            ).fetchone()["n"]
            if count >= self.config.max_sessions:
                raise SessionError(
                    f"session cap reached ({self.config.max_sessions}); finish or cancel an existing session first"
                )
            session_id = uuid.uuid4().hex
            now = iso_now()
            final_name = name or generate_session_name(objective or prompt, mode)
            conn.execute(
                """
                INSERT INTO research_sessions
                    (id, name, status, mode, prompt, objective, marketplaces,
                     phase, progress, created_at, updated_at, last_activity_at)
                VALUES (?, ?, 'draft', ?, ?, ?, ?, 'initializing', 0.0, ?, ?, ?)
                """,
                (
                    session_id,
                    final_name,
                    mode,
                    prompt,
                    objective,
                    json.dumps(marketplaces or []),
                    now,
                    now,
                    now,
                ),
            )
        return self.get(session_id)  # type: ignore[return-value]

    def get(self, session_id: str) -> Session | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM research_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return self._row_to_session(row) if row else None

    def require(self, session_id: str) -> Session:
        s = self.get(session_id)
        if s is None:
            raise SessionError(f"session {session_id} not found")
        return s

    def list(self, include_terminal: bool = True) -> list[Session]:
        q = "SELECT * FROM research_sessions"
        if not include_terminal:
            q += " WHERE status IN ('draft','queued','running','interrupted','paused')"
        q += " ORDER BY created_at DESC"
        with self.db.read() as conn:
            rows = conn.execute(q).fetchall()
        return [self._row_to_session(r) for r in rows]

    # --------------------------------------------------------------- mutate
    def update(
        self,
        session_id: str,
        *,
        name: str | None = None,
        status: str | None = None,
        phase: str | None = None,
        objective: str | None = None,
        progress: float | None = None,
        error: str | None = None,
    ) -> Session:
        s = self.require(session_id)
        if status is not None and status not in STATUSES:
            raise SessionError(f"invalid status {status!r}")
        if phase is not None and phase not in PHASES:
            raise SessionError(f"invalid phase {phase!r}")
        if status == "running":
            # A session may only resume from resumable states.
            if s.status not in ("draft", "queued", "running", "interrupted", "paused"):
                raise SessionError(f"cannot run session in status {s.status!r}")
        fields: dict[str, Any] = {}
        for key, value in (
            ("name", name),
            ("status", status),
            ("phase", phase),
            ("objective", objective),
            ("progress", progress),
            ("error", error),
        ):
            if value is not None:
                fields[key] = value
        if not fields:
            return s
        sets = ", ".join(f"{k} = ?" for k in fields)
        now = iso_now()
        with self.db.tx() as conn:
            conn.execute(
                f"UPDATE research_sessions SET {sets}, updated_at = ?, last_activity_at = ? WHERE id = ?",
                (*fields.values(), now, now, session_id),
            )
        return self.require(session_id)

    def touch(self, session_id: str) -> None:
        now = iso_now()
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE research_sessions SET last_activity_at = ? WHERE id = ?",
                (now, session_id),
            )

    def accumulate_elapsed(self, session_id: str, seconds: float) -> None:
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE research_sessions SET elapsed_seconds = elapsed_seconds + ?, last_activity_at = ? WHERE id = ?",
                (max(0.0, seconds), iso_now(), session_id),
            )

    def resume_count(self, session_id: str) -> int:
        s = self.require(session_id)
        return s.resume_count

    def bump_resume(self, session_id: str) -> None:
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE research_sessions SET resume_count = resume_count + 1 WHERE id = ?",
                (session_id,),
            )

    def counts(self, session_id: str) -> dict[str, int]:
        """Candidate/verification/opportunity counters for the dashboard.
        Single grouped query — this is on the archive-list hot path."""
        with self.db.read() as conn:
            rows = conn.execute(
                """
                SELECT 'c' AS src, status AS k, COUNT(*) AS n FROM candidates
                 WHERE session_id = ? GROUP BY status
                UNION ALL
                SELECT 'o' AS src, 'verified' AS k, COUNT(*) AS n FROM opportunities
                 WHERE session_id = ?
                """,
                (session_id, session_id),
            ).fetchall()
        out = {"discovered": 0, "rejected": 0, "verifying": 0, "verified": 0}
        for r in rows:
            if r["k"] in out:
                out[r["k"]] = int(r["n"])
        return out

    def counts_many(self, session_ids: list[str]) -> dict[str, dict[str, int]]:
        """Batched counters for the archive list: one grouped query for all
        sessions instead of 4N point queries (mobile archive refresh)."""
        result = {sid: {"discovered": 0, "rejected": 0, "verifying": 0, "verified": 0} for sid in session_ids}
        if not session_ids:
            return result
        placeholders = ",".join("?" for _ in session_ids)
        with self.db.read() as conn:
            rows = conn.execute(
                f"""
                SELECT session_id, status AS k, COUNT(*) AS n FROM candidates
                 WHERE session_id IN ({placeholders}) GROUP BY session_id, status
                """,
                session_ids,
            ).fetchall()
            opps = conn.execute(
                f"""
                SELECT session_id, COUNT(*) AS n FROM opportunities
                 WHERE session_id IN ({placeholders}) GROUP BY session_id
                """,
                session_ids,
            ).fetchall()
        for r in rows:
            sid = r["session_id"]
            if sid in result and r["k"] in result[sid]:
                result[sid][r["k"]] = int(r["n"])
        for r in opps:
            sid = r["session_id"]
            if sid in result:
                result[sid]["verified"] = int(r["n"])
        return result

    # ------------------------------------------------------------- recovery
    def mark_interrupted(self, session_id: str, reason: str) -> None:
        s = self.require(session_id)
        if s.status in ("running", "queued"):
            self.update(session_id, status="interrupted", error=reason)

    # ------------------------------------------------------------ helpers
    def _row_to_session(self, row: sqlite3.Row) -> Session:
        return Session(
            id=row["id"],
            name=row["name"],
            status=row["status"],
            mode=row["mode"],
            prompt=row["prompt"],
            objective=row["objective"],
            marketplaces=json.loads(row["marketplaces"]),
            phase=row["phase"],
            progress=row["progress"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_activity_at=row["last_activity_at"],
            elapsed_seconds=row["elapsed_seconds"],
            error=row["error"],
            resume_count=row["resume_count"],
        )


