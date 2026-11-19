"""Observations, evidence, candidates, verifications, opportunities.

These are the research-domain entities. All are session-scoped and persisted
immediately on creation; nothing important lives only in memory.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from app.db import Database
from app.timeutil import iso_now

# Candidate lifecycle
CAND_DISCOVERED = "discovered"
CAND_UNDER_VERIFICATION = "verifying"
CAND_VERIFIED = "verified"
CAND_REJECTED = "rejected"
CANDIDATE_STATUSES = (CAND_DISCOVERED, CAND_UNDER_VERIFICATION, CAND_VERIFIED, CAND_REJECTED)

# Verification outcomes
VER_PASSED = "passed"
VER_FAILED = "failed"
VER_INCONCLUSIVE = "inconclusive"
VER_ERROR = "error"
VERIFICATION_STATUSES = (VER_PASSED, VER_FAILED, VER_INCONCLUSIVE, VER_ERROR)


def new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class Observation:
    id: str
    session_id: str
    created_at: str
    source: str
    kind: str
    content: str
    meta: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "source": self.source,
            "kind": self.kind,
            "content": self.content,
            "meta": self.meta,
        }


@dataclass
class Evidence:
    id: str
    session_id: str
    observation_id: str | None
    created_at: str
    kind: str
    uri: str
    summary: str
    confidence: float
    meta: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "observation_id": self.observation_id,
            "created_at": self.created_at,
            "kind": self.kind,
            "uri": self.uri,
            "summary": self.summary,
            "confidence": self.confidence,
            "meta": self.meta,
        }


@dataclass
class Candidate:
    id: str
    session_id: str
    created_at: str
    updated_at: str
    status: str
    niche: str
    marketplace: str
    rationale: str
    score: float
    meta: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": self.status,
            "niche": self.niche,
            "marketplace": self.marketplace,
            "rationale": self.rationale,
            "score": self.score,
            "meta": self.meta,
        }


@dataclass
class Verification:
    id: str
    session_id: str
    candidate_id: str
    created_at: str
    status: str
    verdict: str
    checks: dict[str, Any]
    confidence: float
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "candidate_id": self.candidate_id,
            "created_at": self.created_at,
            "status": self.status,
            "verdict": self.verdict,
            "checks": self.checks,
            "confidence": self.confidence,
            "error": self.error,
        }


@dataclass
class Opportunity:
    id: str
    session_id: str
    candidate_id: str
    created_at: str
    title: str
    niche: str
    marketplace: str
    angle: str
    keywords: list[str]
    projected_demand: float
    projected_competition: float
    confidence: float
    meta: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "candidate_id": self.candidate_id,
            "created_at": self.created_at,
            "title": self.title,
            "niche": self.niche,
            "marketplace": self.marketplace,
            "angle": self.angle,
            "keywords": self.keywords,
            "projected_demand": self.projected_demand,
            "projected_competition": self.projected_competition,
            "confidence": self.confidence,
            "meta": self.meta,
        }


class ObservationStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(
        self,
        session_id: str,
        *,
        source: str,
        kind: str,
        content: str,
        meta: dict[str, Any] | None = None,
    ) -> Observation:
        obs_id = new_id()
        created_at = iso_now()
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO observations (id, session_id, created_at, source, kind, content, meta) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (obs_id, session_id, created_at, source, kind, content, json.dumps(meta or {})),
            )
        return Observation(
            id=obs_id,
            session_id=session_id,
            created_at=created_at,
            source=source,
            kind=kind,
            content=content,
            meta=meta or {},
        )

    def get(self, session_id: str, obs_id: str) -> Observation | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM observations WHERE session_id = ? AND id = ?",
                (session_id, obs_id),
            ).fetchone()
        return self._row(row) if row else None

    def count(self, session_id: str) -> int:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM observations WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return int(row["n"])

    def list(self, session_id: str, *, limit: int = 500, offset: int = 0) -> list[Observation]:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM observations WHERE session_id = ? ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (session_id, limit, offset),
            ).fetchall()
        return [self._row(r) for r in rows]

    def _row(self, row: sqlite3.Row) -> Observation:
        return Observation(
            id=row["id"],
            session_id=row["session_id"],
            created_at=row["created_at"],
            source=row["source"],
            kind=row["kind"],
            content=row["content"],
            meta=json.loads(row["meta"]),
        )


# Notes are a SHORT annotation channel; the model's long-form record lives in
# app.research_memory. Note TEXT is therefore never truncated (a lossy note is
# worse than no note: the model re-reads its own words later), while the number
# of note rows per session stays bounded.
# 0 = the model may record as many notes as its research needs. The owner's
# rule: no limit on how many times the model may act. (A long run that keeps
# noticing things must never be told its own memory is full.)
MAX_NOTES_PER_SESSION = 0


@dataclass
class ResearchNote:
    """One free-form note the research model wrote during the run."""

    id: str
    session_id: str
    created_at: str
    phase: str
    niche: str
    kind: str
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "phase": self.phase,
            "niche": self.niche,
            "kind": self.kind,
            "text": self.text,
        }


class ResearchNoteStore:
    """Durable notes channel for the researching model.

    The phase output contracts name the fields the backend can store
    structurally (signals, niches, competitive maps, …). Everything else the
    model notices — a dead end, an odd category quirk, an idea that has no
    field yet — would be lost between phases and never reach the report. These
    notes are that escape hatch.

    They are deliberately NOT observations: a note is the model's own
    annotation, so it must never satisfy an evidence/observation floor or any
    quality gate. Identical notes are stored once, and the session total is
    bounded.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    def add(
        self,
        session_id: str,
        *,
        text: str,
        phase: str = "",
        niche: str = "",
        kind: str = "note",
    ) -> tuple[ResearchNote | None, str]:
        """Store one note. Returns (note, outcome).

        outcome is "stored", "duplicate" (identical text already present) or
        "full" (only when an owner sets MAX_NOTES_PER_SESSION; the default is
        no cap at all). Nothing raises
        for a bad note: notes are additive, research never stops for them.
        """
        cleaned = " ".join(str(text or "").split()).strip()
        if not cleaned:
            return None, "empty"
        with self.db.read() as conn:
            existing = conn.execute(
                "SELECT * FROM research_notes WHERE session_id = ? AND text = ? LIMIT 1",
                (session_id, cleaned),
            ).fetchone()
            if existing is not None:
                return self._row(existing), "duplicate"
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM research_notes WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if MAX_NOTES_PER_SESSION and int(total["n"]) >= MAX_NOTES_PER_SESSION:
            return None, "full"
        note = ResearchNote(
            id=new_id(),
            session_id=session_id,
            created_at=iso_now(),
            phase=str(phase or "")[:60],
            niche=str(niche or "")[:200],
            kind=str(kind or "note")[:40],
            text=cleaned,
        )
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO research_notes (id, session_id, created_at, phase, "
                "niche, kind, text) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    note.id, note.session_id, note.created_at, note.phase,
                    note.niche, note.kind, note.text,
                ),
            )
        return note, "stored"

    def list(
        self, session_id: str, *, limit: int = 500, phase: str = ""
    ) -> list[ResearchNote]:
        query = "SELECT * FROM research_notes WHERE session_id = ?"
        params: list[Any] = [session_id]
        if phase:
            query += " AND phase = ?"
            params.append(phase)
        query += " ORDER BY created_at ASC, id ASC LIMIT ?"
        params.append(limit)
        with self.db.read() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row(r) for r in rows]

    def count(self, session_id: str) -> int:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM research_notes WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return int(row["n"])

    def _row(self, row: sqlite3.Row) -> ResearchNote:
        return ResearchNote(
            id=row["id"],
            session_id=row["session_id"],
            created_at=row["created_at"],
            phase=row["phase"],
            niche=row["niche"],
            kind=row["kind"],
            text=row["text"],
        )


class EvidenceStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(
        self,
        session_id: str,
        *,
        kind: str,
        uri: str,
        summary: str,
        observation_id: str | None = None,
        confidence: float = 0.0,
        meta: dict[str, Any] | None = None,
    ) -> Evidence:
        ev_id = new_id()
        created_at = iso_now()
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO evidence (id, session_id, observation_id, created_at, kind, uri, summary, confidence, meta) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ev_id, session_id, observation_id, created_at, kind, uri, summary, confidence, json.dumps(meta or {})),
            )
        return Evidence(
            id=ev_id,
            session_id=session_id,
            observation_id=observation_id,
            created_at=created_at,
            kind=kind,
            uri=uri,
            summary=summary,
            confidence=confidence,
            meta=meta or {},
        )

    def get(self, session_id: str, evidence_id: str) -> Evidence | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM evidence WHERE session_id = ? AND id = ?",
                (session_id, evidence_id),
            ).fetchone()
        return self._row(row) if row else None

    def list(self, session_id: str, *, limit: int = 500, offset: int = 0) -> list[Evidence]:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM evidence WHERE session_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (session_id, limit, offset),
            ).fetchall()
        return [self._row(r) for r in rows]

    def for_observation(self, session_id: str, observation_id: str) -> list[Evidence]:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM evidence WHERE session_id = ? AND observation_id = ? ORDER BY created_at DESC",
                (session_id, observation_id),
            ).fetchall()
        return [self._row(r) for r in rows]

    def _row(self, row: sqlite3.Row) -> Evidence:
        return Evidence(
            id=row["id"],
            session_id=row["session_id"],
            observation_id=row["observation_id"],
            created_at=row["created_at"],
            kind=row["kind"],
            uri=row["uri"],
            summary=row["summary"],
            confidence=row["confidence"],
            meta=json.loads(row["meta"]),
        )


class CandidateStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(
        self,
        session_id: str,
        *,
        niche: str,
        marketplace: str = "",
        rationale: str = "",
        score: float = 0.0,
        meta: dict[str, Any] | None = None,
    ) -> Candidate:
        cand_id = new_id()
        now = iso_now()
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO candidates (id, session_id, created_at, updated_at, status, niche, marketplace, rationale, score, meta) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (cand_id, session_id, now, now, CAND_DISCOVERED, niche, marketplace, rationale, score, json.dumps(meta or {})),
            )
        return self.get(session_id, cand_id)  # type: ignore[return-value]

    def get(self, session_id: str, cand_id: str) -> Candidate | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM candidates WHERE session_id = ? AND id = ?",
                (session_id, cand_id),
            ).fetchone()
        return self._row(row) if row else None

    def require(self, session_id: str, cand_id: str) -> Candidate:
        c = self.get(session_id, cand_id)
        if c is None:
            raise ValueError(f"candidate {cand_id} not found in session {session_id}")
        return c

    def list(self, session_id: str, *, status: str | None = None, limit: int = 500) -> list[Candidate]:
        q = "SELECT * FROM candidates WHERE session_id = ?"
        params: list[Any] = [session_id]
        if status is not None:
            if status not in CANDIDATE_STATUSES:
                raise ValueError(f"invalid candidate status {status!r}")
            q += " AND status = ?"
            params.append(status)
        q += " ORDER BY score DESC, created_at DESC LIMIT ?"
        params.append(limit)
        with self.db.read() as conn:
            rows = conn.execute(q, params).fetchall()
        return [self._row(r) for r in rows]

    def set_status(self, session_id: str, cand_id: str, status: str) -> Candidate:
        if status not in CANDIDATE_STATUSES:
            raise ValueError(f"invalid candidate status {status!r}")
        self.require(session_id, cand_id)
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE candidates SET status = ?, updated_at = ? WHERE session_id = ? AND id = ?",
                (status, iso_now(), session_id, cand_id),
            )
        return self.get(session_id, cand_id)  # type: ignore[return-value]

    def update_score(self, session_id: str, cand_id: str, score: float) -> Candidate:
        self.require(session_id, cand_id)
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE candidates SET score = ?, updated_at = ? WHERE session_id = ? AND id = ?",
                (score, iso_now(), session_id, cand_id),
            )
        return self.get(session_id, cand_id)  # type: ignore[return-value]

    def update_meta(
        self, session_id: str, cand_id: str, meta: dict[str, Any], *, merge: bool = True
    ) -> Candidate:
        """Persist derived per-candidate analysis (e.g. the whitespace score).

        `merge=True` (default) shallow-merges into the existing meta so
        concurrent writers (chain, parent_market, seed evidence) survive.
        """
        cand = self.require(session_id, cand_id)
        new_meta = {**cand.meta, **meta} if merge else dict(meta)
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE candidates SET meta = ?, updated_at = ? WHERE session_id = ? AND id = ?",
                (json.dumps(new_meta), iso_now(), session_id, cand_id),
            )
        return self.get(session_id, cand_id)  # type: ignore[return-value]

    def update_fields(
        self,
        session_id: str,
        cand_id: str,
        *,
        niche: str | None = None,
        marketplace: str | None = None,
        rationale: str | None = None,
        score: float | None = None,
        status: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Candidate:
        """Edit a research record's own fields (any subset; `meta` merges).

        A candidate is not write-once: after reading more evidence the model
        may narrow the niche, correct the marketplace, re-word the rationale,
        re-score it or change its status. Passing no field returns the row
        unchanged. `meta` shallow-merges so chain/parent/whitespace bookkeeping
        survives the edit.
        """
        cand = self.require(session_id, cand_id)
        sets: list[str] = []
        params: list[Any] = []
        if niche is not None:
            clean = " ".join(str(niche).split())
            if not clean:
                raise ValueError("candidate niche cannot be empty")
            sets.append("niche = ?")
            params.append(clean[:200])
        if marketplace is not None:
            sets.append("marketplace = ?")
            params.append(str(marketplace).strip()[:60])
        if rationale is not None:
            sets.append("rationale = ?")
            params.append(str(rationale).strip()[:4000])
        if score is not None:
            sets.append("score = ?")
            params.append(float(score))
        if status is not None:
            if status not in CANDIDATE_STATUSES:
                raise ValueError(f"invalid candidate status {status!r}")
            sets.append("status = ?")
            params.append(status)
        if meta is not None:
            sets.append("meta = ?")
            params.append(json.dumps({**cand.meta, **meta}))
        if not sets:
            return cand
        sets.append("updated_at = ?")
        params.append(iso_now())
        params.extend([session_id, cand_id])
        with self.db.tx() as conn:
            conn.execute(
                f"UPDATE candidates SET {', '.join(sets)} WHERE session_id = ? AND id = ?",
                params,
            )
        return self.get(session_id, cand_id)  # type: ignore[return-value]

    def delete(self, session_id: str, cand_id: str) -> dict[str, int]:
        """Remove a candidate AND the rows that hang off it, atomically.

        Verifications and opportunities reference the candidate through real
        foreign keys, so they go with it; claims keep their own rows but lose
        the candidate link (a claim is about evidence, not about this line of
        research). Returns what was removed so the run's trace can say it.
        """
        self.require(session_id, cand_id)
        with self.db.tx() as conn:
            verifications = conn.execute(
                "DELETE FROM verifications WHERE session_id = ? AND candidate_id = ?",
                (session_id, cand_id),
            ).rowcount
            opportunities = conn.execute(
                "DELETE FROM opportunities WHERE session_id = ? AND candidate_id = ?",
                (session_id, cand_id),
            ).rowcount
            conn.execute(
                "UPDATE claims SET candidate_id = NULL WHERE session_id = ? AND candidate_id = ?",
                (session_id, cand_id),
            )
            conn.execute(
                "DELETE FROM candidates WHERE session_id = ? AND id = ?",
                (session_id, cand_id),
            )
        return {
            "verifications": max(0, int(verifications)),
            "opportunities": max(0, int(opportunities)),
        }

    def _row(self, row: sqlite3.Row) -> Candidate:
        return Candidate(
            id=row["id"],
            session_id=row["session_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            status=row["status"],
            niche=row["niche"],
            marketplace=row["marketplace"],
            rationale=row["rationale"],
            score=row["score"],
            meta=json.loads(row["meta"]),
        )


class VerificationStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(
        self,
        session_id: str,
        *,
        candidate_id: str,
    ) -> Verification:
        ver_id = new_id()
        created_at = iso_now()
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO verifications (id, session_id, candidate_id, created_at, status) VALUES (?, ?, ?, ?, 'pending')",
                (ver_id, session_id, candidate_id, created_at),
            )
        return self.get(session_id, ver_id)  # type: ignore[return-value]

    def get(self, session_id: str, ver_id: str) -> Verification | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM verifications WHERE session_id = ? AND id = ?",
                (session_id, ver_id),
            ).fetchone()
        return self._row(row) if row else None

    def complete(
        self,
        session_id: str,
        ver_id: str,
        *,
        status: str,
        verdict: str = "",
        checks: dict[str, Any] | None = None,
        confidence: float = 0.0,
        error: str | None = None,
    ) -> Verification:
        if status not in VERIFICATION_STATUSES:
            raise ValueError(f"invalid verification status {status!r}")
        v = self.get(session_id, ver_id)
        if v is None:
            raise ValueError(f"verification {ver_id} not found in session {session_id}")
        with self.db.tx() as conn:
            conn.execute(
                """
                UPDATE verifications
                SET status = ?, verdict = ?, checks = ?, confidence = ?, error = ?
                WHERE session_id = ? AND id = ?
                """,
                (status, verdict, json.dumps(checks or {}), confidence, error, session_id, ver_id),
            )
        return self.get(session_id, ver_id)  # type: ignore[return-value]

    def list(self, session_id: str, *, limit: int = 500) -> list[Verification]:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM verifications WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [self._row(r) for r in rows]

    def for_candidate(self, session_id: str, candidate_id: str) -> list[Verification]:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM verifications WHERE session_id = ? AND candidate_id = ? ORDER BY created_at DESC",
                (session_id, candidate_id),
            ).fetchall()
        return [self._row(r) for r in rows]

    def _row(self, row: sqlite3.Row) -> Verification:
        return Verification(
            id=row["id"],
            session_id=row["session_id"],
            candidate_id=row["candidate_id"],
            created_at=row["created_at"],
            status=row["status"],
            verdict=row["verdict"],
            checks=json.loads(row["checks"]),
            confidence=row["confidence"],
            error=row["error"],
        )


class OpportunityStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(
        self,
        session_id: str,
        *,
        candidate_id: str,
        title: str,
        niche: str,
        marketplace: str = "",
        angle: str = "",
        keywords: list[str] | None = None,
        projected_demand: float = 0.0,
        projected_competition: float = 0.0,
        confidence: float = 0.0,
        meta: dict[str, Any] | None = None,
    ) -> Opportunity:
        opp_id = new_id()
        created_at = iso_now()
        with self.db.tx() as conn:
            conn.execute(
                """
                INSERT INTO opportunities
                    (id, session_id, candidate_id, created_at, title, niche, marketplace,
                     angle, keywords, projected_demand, projected_competition, confidence, meta)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    opp_id,
                    session_id,
                    candidate_id,
                    created_at,
                    title,
                    niche,
                    marketplace,
                    angle,
                    json.dumps(keywords or []),
                    projected_demand,
                    projected_competition,
                    confidence,
                    json.dumps(meta or {}),
                ),
            )
        return self.get(session_id, opp_id)  # type: ignore[return-value]

    def get(self, session_id: str, opp_id: str) -> Opportunity | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM opportunities WHERE session_id = ? AND id = ?",
                (session_id, opp_id),
            ).fetchone()
        return self._row(row) if row else None

    def list(self, session_id: str, *, limit: int = 500) -> list[Opportunity]:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM opportunities WHERE session_id = ? ORDER BY confidence DESC, created_at DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [self._row(r) for r in rows]

    def count(self, session_id: str) -> int:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM opportunities WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return int(row["n"])

    def update_fields(
        self,
        session_id: str,
        opp_id: str,
        *,
        title: str | None = None,
        niche: str | None = None,
        marketplace: str | None = None,
        angle: str | None = None,
        keywords: list[str] | None = None,
        projected_demand: float | None = None,
        projected_competition: float | None = None,
        confidence: float | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Opportunity:
        """Edit an opportunity's own fields (any subset; `meta` merges)."""
        opp = self.get(session_id, opp_id)
        if opp is None:
            raise ValueError(f"opportunity {opp_id} not found in session {session_id}")
        sets: list[str] = []
        params: list[Any] = []
        if title is not None:
            clean = " ".join(str(title).split())
            if not clean:
                raise ValueError("opportunity title cannot be empty")
            sets.append("title = ?")
            params.append(clean[:200])
        if niche is not None:
            sets.append("niche = ?")
            params.append(str(niche).strip()[:200])
        if marketplace is not None:
            sets.append("marketplace = ?")
            params.append(str(marketplace).strip()[:60])
        if angle is not None:
            sets.append("angle = ?")
            params.append(str(angle).strip()[:1000])
        if keywords is not None:
            clean_kw = [str(k).strip()[:80] for k in keywords if str(k).strip()]
            sets.append("keywords = ?")
            params.append(json.dumps(clean_kw[:40]))
        if projected_demand is not None:
            sets.append("projected_demand = ?")
            params.append(float(projected_demand))
        if projected_competition is not None:
            sets.append("projected_competition = ?")
            params.append(float(projected_competition))
        if confidence is not None:
            sets.append("confidence = ?")
            params.append(float(confidence))
        if meta is not None:
            sets.append("meta = ?")
            params.append(json.dumps({**opp.meta, **meta}))
        if not sets:
            return opp
        params.extend([session_id, opp_id])
        with self.db.tx() as conn:
            conn.execute(
                f"UPDATE opportunities SET {', '.join(sets)} WHERE session_id = ? AND id = ?",
                params,
            )
        return self.get(session_id, opp_id)  # type: ignore[return-value]

    def delete(self, session_id: str, opp_id: str) -> bool:
        if self.get(session_id, opp_id) is None:
            return False
        with self.db.tx() as conn:
            conn.execute(
                "DELETE FROM opportunities WHERE session_id = ? AND id = ?",
                (session_id, opp_id),
            )
        return True

    def _row(self, row: sqlite3.Row) -> Opportunity:
        return Opportunity(
            id=row["id"],
            session_id=row["session_id"],
            candidate_id=row["candidate_id"],
            created_at=row["created_at"],
            title=row["title"],
            niche=row["niche"],
            marketplace=row["marketplace"],
            angle=row["angle"],
            keywords=json.loads(row["keywords"]),
            projected_demand=row["projected_demand"],
            projected_competition=row["projected_competition"],
            confidence=row["confidence"],
            meta=json.loads(row["meta"]),
        )



