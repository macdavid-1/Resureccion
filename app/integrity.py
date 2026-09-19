"""Durable integrity assessments.

Every integrity decision the system makes — filter outcomes, verification
results, quality-gate outcomes, KDP risk screenings, report-validation
passes — is persisted as an `integrity_assessment` row so the owner can
audit exactly WHY any candidate survived or died. Nothing is cosmetic: an
assessment names the gate/filter that produced it, the numeric evidence it
measured, and the human-readable reason.

Assessment kinds:
    filter          candidate filter outcome (app.filters)
    verification    verification engine outcome (app.verification)
    quality_gate    one of the 7 research quality gates (app.quality_gates)
    kdp_risk        KDP risk screening (app.kdp_risk)
    report_validation  adversarial report validation pass (app.report_validation)
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from app.db import Database
from app.timeutil import iso_now

KIND_FILTER = "filter"
KIND_VERIFICATION = "verification"
KIND_QUALITY_GATE = "quality_gate"
KIND_KDP_RISK = "kdp_risk"
KIND_KEYWORD_INTEGRITY = "keyword_integrity"
KIND_REPORT_VALIDATION = "report_validation"
ASSESSMENT_KINDS = (
    KIND_FILTER, KIND_VERIFICATION, KIND_QUALITY_GATE,
    KIND_KDP_RISK, KIND_KEYWORD_INTEGRITY, KIND_REPORT_VALIDATION,
)


@dataclass
class IntegrityAssessment:
    id: int
    session_id: str
    created_at: str
    kind: str
    subject_type: str        # candidate | opportunity | report | session
    subject_id: str
    stage: str               # e.g. "insufficient_evidence", "Evidence Gate", ...
    outcome: str             # pass | fail | warn | pending
    reason: str
    metrics: dict[str, Any]  # the measured numbers behind the decision
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "kind": self.kind,
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "stage": self.stage,
            "outcome": self.outcome,
            "reason": self.reason,
            "metrics": self.metrics,
            "meta": self.meta,
        }


class IntegrityAssessmentStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def record(
        self,
        session_id: str,
        *,
        kind: str,
        subject_type: str,
        subject_id: str,
        stage: str,
        outcome: str,
        reason: str,
        metrics: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> IntegrityAssessment:
        if kind not in ASSESSMENT_KINDS:
            raise ValueError(f"invalid assessment kind {kind!r}")
        if outcome not in ("pass", "fail", "warn", "pending"):
            raise ValueError(f"invalid assessment outcome {outcome!r}")
        with self.db.tx() as conn:
            cur = conn.execute(
                """
                INSERT INTO integrity_assessments
                    (session_id, created_at, kind, subject_type, subject_id,
                     stage, outcome, reason, metrics, meta)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id, iso_now(), kind, subject_type, subject_id,
                    stage, outcome, reason[:1000],
                    json.dumps(metrics or {}), json.dumps(meta or {}),
                ),
            )
            aid = int(cur.lastrowid)
        return self.get(session_id, aid)  # type: ignore[return-value]

    def get(self, session_id: str, assessment_id: int) -> IntegrityAssessment | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM integrity_assessments WHERE session_id = ? AND id = ?",
                (session_id, assessment_id),
            ).fetchone()
        return self._row(row) if row else None

    def list(
        self,
        session_id: str,
        *,
        kind: str | None = None,
        subject_id: str | None = None,
        outcome: str | None = None,
        limit: int = 500,
    ) -> list[IntegrityAssessment]:
        q = "SELECT * FROM integrity_assessments WHERE session_id = ?"
        params: list[Any] = [session_id]
        if kind is not None:
            q += " AND kind = ?"
            params.append(kind)
        if subject_id is not None:
            q += " AND subject_id = ?"
            params.append(subject_id)
        if outcome is not None:
            q += " AND outcome = ?"
            params.append(outcome)
        q += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self.db.read() as conn:
            rows = conn.execute(q, params).fetchall()
        return [self._row(r) for r in rows]

    def _row(self, row: sqlite3.Row) -> IntegrityAssessment:
        return IntegrityAssessment(
            id=row["id"],
            session_id=row["session_id"],
            created_at=row["created_at"],
            kind=row["kind"],
            subject_type=row["subject_type"],
            subject_id=row["subject_id"],
            stage=row["stage"],
            outcome=row["outcome"],
            reason=row["reason"],
            metrics=json.loads(row["metrics"]),
            meta=json.loads(row["meta"]),
        )
