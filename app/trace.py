"""Operational activity trace: transparency without exposing chain-of-thought.

The owner must be able to open Resurrección at any time and see WHAT the
agent is doing and WHY in one line — synchronized with the live session —
without exposing the model's private reasoning.

`ActivityTrace.record()` stores concise, operator-style summaries like:

    «Investigating competitor cluster around X because multiple independent
     demand signals were observed.»

    «Rejecting candidate Y because the apparent demand is concentrated in
     one anomalous title.»

The research runner emits these at every meaningful transition (phase
enter/exit, candidate created/rejected/verified, pivot, marketplace switch,
verification outcome). The UI renders the newest entries live. Raw events
remain available separately for full auditing.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from app.db import Database
from app.timeutil import iso_now

# Trace kinds (operator-facing categories).
KIND_PHASE = "phase"
KIND_DISCOVERY = "discovery"
KIND_CANDIDATE = "candidate"
KIND_VERIFICATION = "verification"
KIND_PIVOT = "pivot"
KIND_BROWSER = "browser"
KIND_REPORT = "report"
KIND_RECOVERY = "recovery"

KINDS = (KIND_PHASE, KIND_DISCOVERY, KIND_CANDIDATE, KIND_VERIFICATION,
         KIND_PIVOT, KIND_BROWSER, KIND_REPORT, KIND_RECOVERY)


@dataclass
class TraceEntry:
    id: int
    session_id: str
    created_at: str
    kind: str
    text: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "kind": self.kind,
            "text": self.text,
            "detail": self.detail,
        }


class ActivityTrace:
    """Durable, concise operational trace for one session."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def record(self, session_id: str, kind: str, text: str, *, detail: dict[str, Any] | None = None) -> TraceEntry:
        if kind not in KINDS:
            raise ValueError(f"invalid trace kind {kind!r}")
        text = " ".join(str(text).split())[:400]
        now = iso_now()
        with self.db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO activity_trace (session_id, created_at, kind, text, detail) VALUES (?, ?, ?, ?, ?)",
                (session_id, now, kind, text, json.dumps(redact_detail(detail or {}))),
            )
            entry_id = int(cur.lastrowid)
        return TraceEntry(id=entry_id, session_id=session_id, created_at=now, kind=kind, text=text,
                          detail=redact_detail(detail or {}))

    def recent(self, session_id: str, *, after_id: int = 0, limit: int = 50) -> list[TraceEntry]:
        limit = max(1, min(limit, 200))
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM activity_trace WHERE session_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
                (session_id, after_id, limit),
            ).fetchall()
        return [self._row(r) for r in rows]

    def count(self, session_id: str) -> int:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM activity_trace WHERE session_id = ?", (session_id,)
            ).fetchone()
        return int(row["n"]) if row else 0

    def _row(self, row: sqlite3.Row) -> TraceEntry:
        return TraceEntry(
            id=row["id"],
            session_id=row["session_id"],
            created_at=row["created_at"],
            kind=row["kind"],
            text=row["text"],
            detail=json.loads(row["detail"]),
        )


def redact_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """Trace details are owner-facing: reuse the redaction hygiene layer."""
    from app.redact import redact

    return redact(detail)


# ---------------------------------------------------------------------- helpers
def candidate_created_text(niche: str, marketplace: str, rationale: str) -> str:
    mkt = f" on {marketplace}" if marketplace else ""
    why = f" — {rationale}" if rationale else ""
    return f"Candidate created: {niche}{mkt}{why}"


def candidate_rejected_text(niche: str, reasons: list[str]) -> str:
    why = "; ".join(str(r) for r in reasons[:2]) if reasons else "integrity filters"
    return f"Rejecting candidate {niche!r} because {why}"


def candidate_verified_text(niche: str, confidence: float) -> str:
    return f"Candidate {niche!r} verified (confidence {round(confidence * 100)}%) after adversarial checks"


def phase_text(phase_title: str, entering: bool) -> str:
    verb = "Entering" if entering else "Completed"
    return f"{verb} {phase_title}"


def pivot_text(phase_title: str, reason: str) -> str:
    return f"Pivoting within {phase_title}: {reason}"


def marketplace_text(codes: list[str], mode: str) -> str:
    if mode == "explicit":
        return f"Researching owner-specified marketplaces: {', '.join(codes)}"
    return f"Auto-selected marketplaces by relevance: {', '.join(codes)}"


def browser_text(activity: str, marketplace: str, url: str | None = None) -> str:
    mkt = f" [{marketplace}]" if marketplace else ""
    u = f" — {url}" if url else ""
    return f"Browser: {activity}{mkt}{u}"


def verification_text(niche: str, verdict: str, problems: list[str]) -> str:
    if verdict == "verified":
        return f"Verification passed for {niche!r}: all deterministic checks passed"
    if verdict == "verified_with_limitations":
        return f"Verification passed with limitations for {niche!r}: {'; '.join(problems[:2])}"
    if verdict == "inconclusive":
        return f"Verification inconclusive for {niche!r}: needs more research ({'; '.join(problems[:2])})"
    return f"Verification rejected {niche!r}: {'; '.join(problems[:2])}"


def recovery_text(what: str, action: str) -> str:
    return f"Recovered from {what}: {action}"
