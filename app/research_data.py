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



