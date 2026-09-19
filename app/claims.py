"""The claim layer: observation -> evidence -> claim -> interpretation ->
verification -> conclusion.

This module enforces the evidence chain-of-custody that keeps Resurrección
honest. The model is free to reason, but any *important factual claim* it
wants carried into a final report must be registered here as a `Claim`
pointing at the evidence ids it rests on. Unsupported model assumptions may
exist in the transcript; they may NOT silently become report facts.

Evidence quality classes (deterministic ranking — do not present inference
as direct evidence):

    direct_observation   captured from a marketplace page the agent visited
    extension_derived    read from the KDSpy extension's rendered panel
    repeated_observation the same fact seen on 2+ independent captures
    external_corroboration fact confirmed across marketplaces/sources
    model_inference      the model's reasoning, no page behind it
    uncertain_observation page data was ambiguous/incomplete at capture

Reliability weights are used to compute claim confidence and to compute the
supported-claim ratio used by the quality gates.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from app.db import Database
from app.redact import redact
from app.timeutil import iso_now

# Evidence quality classes, ranked best -> worst.
EQ_DIRECT = "direct_observation"
EQ_EXTENSION = "extension_derived"
EQ_REPEATED = "repeated_observation"
EQ_CORROBORATED = "external_corroboration"
EQ_INFERENCE = "model_inference"
EQ_UNCERTAIN = "uncertain_observation"

EVIDENCE_QUALITY_CLASSES: tuple[str, ...] = (
    EQ_DIRECT,
    EQ_EXTENSION,
    EQ_REPEATED,
    EQ_CORROBORATED,
    EQ_INFERENCE,
    EQ_UNCERTAIN,
)

# Reliability weight per class (0.0-1.0). Used for claim confidence math.
EVIDENCE_QUALITY_WEIGHTS: dict[str, float] = {
    EQ_DIRECT: 1.0,
    EQ_EXTENSION: 0.95,
    EQ_REPEATED: 0.9,
    EQ_CORROBORATED: 0.85,
    EQ_INFERENCE: 0.25,
    EQ_UNCERTAIN: 0.4,
}

# Claim lifecycle.
CLAIM_OPEN = "open"
CLAIM_SUPPORTED = "supported"
CLAIM_UNSUPPORTED = "unsupported"
CLAIM_CONTRADICTED = "contradicted"
CLAIM_STATUSES = (CLAIM_OPEN, CLAIM_SUPPORTED, CLAIM_UNSUPPORTED, CLAIM_CONTRADICTED)

# What kind of factual matter the claim asserts.
CLAIM_KINDS = (
    "demand",            # "readers buy X" / ranking/review-velocity claims
    "competition",       # competitor count/pricing/saturation claims
    "consumer_need",     # review-derived reader wants/complaints
    "market_gap",        # "competitors fail to provide X"
    "risk",              # KDP policy/compliance/volatility risks
    "metadata",          # keyword/title integrity facts
    "other",
)

# Verification verdicts (aligned with the verification engine).
VERDICT_VERIFIED = "verified"
VERDICT_LIMITED = "verified_with_limitations"
VERDICT_INCONCLUSIVE = "inconclusive"
VERDICT_REJECTED = "rejected"
CLAIM_VERDICTS = (VERDICT_VERIFIED, VERDICT_LIMITED, VERDICT_INCONCLUSIVE, VERDICT_REJECTED)


class ClaimError(Exception):
    pass


def evidence_quality_from_kind(kind: str) -> str:
    """Map a browser-evidence `kind` onto an evidence quality class.

    Deterministic: only actually-captured page data can be 'direct';
    KDSpy-derived records are 'extension_derived'. Everything the model
    asserts without a capture is 'model_inference' by construction.
    """
    mapping = {
        "search_results": EQ_DIRECT,
        "product_page": EQ_DIRECT,
        "reviews": EQ_DIRECT,
        "autocomplete": EQ_DIRECT,
        "related_products": EQ_DIRECT,
        "kdspy_panel": EQ_EXTENSION,
        "page_state": EQ_DIRECT,
    }
    return mapping.get(kind, EQ_UNCERTAIN)


@dataclass
class Claim:
    id: str
    session_id: str
    candidate_id: str | None
    created_at: str
    kind: str
    statement: str
    evidence_ids: list[str]
    quality: str           # best evidence class among evidence_ids
    confidence: float      # computed from quality + evidence spread
    status: str            # supported / unsupported / contradicted
    verdict: str | None    # verification verdict once verified
    interpretation: str    # what the claim means for the research
    meta: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "candidate_id": self.candidate_id,
            "created_at": self.created_at,
            "kind": self.kind,
            "statement": self.statement,
            "evidence_ids": self.evidence_ids,
            "quality": self.quality,
            "confidence": self.confidence,
            "status": self.status,
            "verdict": self.verdict,
            "interpretation": self.interpretation,
            "meta": redact(self.meta),
        }


def compute_claim_confidence(evidence_ids: list[str], quality: str) -> float:
    """Deterministic confidence from evidence spread + quality class."""
    base = EVIDENCE_QUALITY_WEIGHTS.get(quality, 0.4)
    # Small bonus for independent corroboration (more than one evidence id),
    # capped so quantity can never fake quality.
    spread_bonus = min(0.15, 0.05 * max(0, len(evidence_ids) - 1))
    return round(min(1.0, base + spread_bonus), 3)


class ClaimStore:
    """Durable store of claims linking evidence -> factual assertions."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def create(
        self,
        session_id: str,
        *,
        kind: str,
        statement: str,
        evidence_ids: list[str] | None = None,
        quality: str | None = None,
        candidate_id: str | None = None,
        interpretation: str = "",
        meta: dict[str, Any] | None = None,
    ) -> Claim:
        if kind not in CLAIM_KINDS:
            raise ClaimError(f"invalid claim kind {kind!r}")
        evidence_ids = [str(e) for e in (evidence_ids or []) if str(e).strip()]
        if quality is None:
            quality = EQ_INFERENCE if not evidence_ids else _best_quality(evidence_ids)
        if quality not in EVIDENCE_QUALITY_CLASSES:
            raise ClaimError(f"invalid evidence quality class {quality!r}")
        statement = statement.strip()
        if not statement:
            raise ClaimError("claim statement must be non-empty")
        # Status is deterministic: no evidence ids -> unsupported by design.
        status = CLAIM_SUPPORTED if evidence_ids else CLAIM_UNSUPPORTED
        claim = Claim(
            id=_new_id(),
            session_id=session_id,
            candidate_id=candidate_id,
            created_at=iso_now(),
            kind=kind,
            statement=statement[:600],
            evidence_ids=evidence_ids,
            quality=quality,
            confidence=compute_claim_confidence(evidence_ids, quality),
            status=status,
            verdict=None,
            interpretation=interpretation[:600],
            meta=meta or {},
        )
        with self.db.tx() as conn:
            conn.execute(
                """
                INSERT INTO claims (id, session_id, candidate_id, created_at, kind,
                                    statement, evidence_ids, quality, confidence,
                                    status, verdict, interpretation, meta)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    claim.id, claim.session_id, claim.candidate_id, claim.created_at,
                    claim.kind, claim.statement, json.dumps(claim.evidence_ids),
                    claim.quality, claim.confidence, claim.status, claim.verdict,
                    claim.interpretation, json.dumps(claim.meta),
                ),
            )
        return claim

    def get(self, session_id: str, claim_id: str) -> Claim | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM claims WHERE session_id = ? AND id = ?",
                (session_id, claim_id),
            ).fetchone()
        return self._row(row) if row else None

    def list(
        self,
        session_id: str,
        *,
        candidate_id: str | None = None,
        status: str | None = None,
        kind: str | None = None,
        limit: int = 500,
    ) -> list[Claim]:
        q = "SELECT * FROM claims WHERE session_id = ?"
        params: list[Any] = [session_id]
        if candidate_id is not None:
            q += " AND candidate_id = ?"
            params.append(candidate_id)
        if status is not None:
            q += " AND status = ?"
            params.append(status)
        if kind is not None:
            q += " AND kind = ?"
            params.append(kind)
        q += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self.db.read() as conn:
            rows = conn.execute(q, params).fetchall()
        return [self._row(r) for r in rows]

    def for_evidence(self, session_id: str, evidence_id: str) -> list[Claim]:
        """Claims referencing an evidence id (JSON array containment)."""
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM claims WHERE session_id = ? AND instr(evidence_ids, ?) > 0",
                (session_id, f'"{evidence_id}"'),
            ).fetchall()
        out = []
        for r in rows:
            c = self._row(r)
            if evidence_id in c.evidence_ids:
                out.append(c)
        return out

    def set_verdict(self, session_id: str, claim_id: str, verdict: str) -> Claim:
        if verdict not in CLAIM_VERDICTS:
            raise ClaimError(f"invalid claim verdict {verdict!r}")
        if self.get(session_id, claim_id) is None:
            raise ClaimError(f"claim {claim_id} not found in session {session_id}")
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE claims SET verdict = ? WHERE session_id = ? AND id = ?",
                (verdict, session_id, claim_id),
            )
        return self.get(session_id, claim_id)  # type: ignore[return-value]

    def set_status(self, session_id: str, claim_id: str, status: str) -> Claim:
        if status not in CLAIM_STATUSES:
            raise ClaimError(f"invalid claim status {status!r}")
        if self.get(session_id, claim_id) is None:
            raise ClaimError(f"claim {claim_id} not found in session {session_id}")
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE claims SET status = ? WHERE session_id = ? AND id = ?",
                (status, session_id, claim_id),
            )
        return self.get(session_id, claim_id)  # type: ignore[return-value]

    def count(self, session_id: str, *, status: str | None = None) -> int:
        q = "SELECT COUNT(*) AS n FROM claims WHERE session_id = ?"
        params: list[Any] = [session_id]
        if status is not None:
            q += " AND status = ?"
            params.append(status)
        with self.db.read() as conn:
            row = conn.execute(q, params).fetchone()
        return int(row["n"])

    def _row(self, row: sqlite3.Row) -> Claim:
        return Claim(
            id=row["id"],
            session_id=row["session_id"],
            candidate_id=row["candidate_id"],
            created_at=row["created_at"],
            kind=row["kind"],
            statement=row["statement"],
            evidence_ids=json.loads(row["evidence_ids"]),
            quality=row["quality"],
            confidence=row["confidence"],
            status=row["status"],
            verdict=row["verdict"],
            interpretation=row["interpretation"],
            meta=json.loads(row["meta"]),
        )


def _best_quality(evidence_ids: list[str]) -> str:
    """Best (highest-reliability) quality derivable from evidence count alone.

    Cross-store validation of actual evidence kinds happens in the integrity
    engine; this default assumes direct observations were captured (the only
    way evidence ids exist in a session).
    """
    if len(evidence_ids) >= 2:
        return EQ_REPEATED
    return EQ_DIRECT


def _new_id() -> str:
    import uuid

    return uuid.uuid4().hex
