"""Export jobs.

Exports package a session's research output (report markdown, evidence,
opportunities, events) into a downloadable archive. Export jobs are durable:
the owner can queue one, the server can crash, and the job resumes/retries
without loss (pending/running jobs found at boot are re-executed on demand).
"""
from __future__ import annotations

import io
import json
import sqlite3
import tarfile
import time as _time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import Config
from app.db import Database, atomic_write_bytes, atomic_write_text
from app.events import EventLog
from app.recovery import CheckpointStore
from app.reports import ReportStore
from app.research_data import (
    CandidateStore,
    EvidenceStore,
    ObservationStore,
    OpportunityStore,
)
from app.redact import redact
from app.timeutil import iso_now

EXPORT_FORMATS = ("tar_gz", "json")


class ExportError(Exception):
    pass


@dataclass
class ExportJob:
    id: str
    session_id: str
    created_at: str
    updated_at: str
    status: str
    format: str
    error: str | None
    result_path: str | None
    attempts: int
    started_at: str | None
    finished_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": self.status,
            "format": self.format,
            "error": self.error,
            "result_path": self.result_path,
            "attempts": self.attempts,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class ExportManager:
    def __init__(
        self,
        db: Database,
        config: Config,
        reports: ReportStore,
        candidates: CandidateStore,
        opportunities: OpportunityStore,
        observations: ObservationStore | None = None,
        evidence: EvidenceStore | None = None,
        events: EventLog | None = None,
        checkpoints: CheckpointStore | None = None,
    ) -> None:
        self.db = db
        self.config = config
        self.reports = reports
        self.candidates = candidates
        self.opportunities = opportunities
        self.observations = observations
        self.evidence = evidence
        self.events = events
        self.checkpoints = checkpoints

    def session_dir(self, session_id: str) -> Path:
        return self.config.exports_dir / session_id

    # ------------------------------------------------------------- job rows
    def create(self, session_id: str, *, format: str = "tar_gz") -> ExportJob:
        if format not in EXPORT_FORMATS:
            raise ExportError(f"invalid export format {format!r}")
        export_id = uuid.uuid4().hex
        now = iso_now()
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO export_jobs (id, session_id, created_at, updated_at, status, format) VALUES (?, ?, ?, ?, 'pending', ?)",
                (export_id, session_id, now, now, format),
            )
        return self.get(session_id, export_id)  # type: ignore[return-value]

    def get(self, session_id: str, export_id: str) -> ExportJob | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM export_jobs WHERE session_id = ? AND id = ?",
                (session_id, export_id),
            ).fetchone()
        return self._row(row) if row else None

    def list(self, session_id: str) -> list[ExportJob]:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM export_jobs WHERE session_id = ? ORDER BY created_at DESC",
                (session_id,),
            ).fetchall()
        return [self._row(r) for r in rows]

    def _set_status(
        self, session_id: str, export_id: str, *, status: str, error: str | None = None, result_path: str | None = None
    ) -> ExportJob:
        now = iso_now()
        sets = "status = ?, updated_at = ?"
        params: list[Any] = [status, now]
        if status == "running":
            sets += ", started_at = COALESCE(started_at, ?), attempts = attempts + 1"
            params.append(now)
        elif status in ("succeeded", "failed"):
            sets += ", finished_at = ?"
            params.append(now)
        if error is not None:
            sets += ", error = ?"
            params.append(error)
        if result_path is not None:
            sets += ", result_path = ?"
            params.append(result_path)
        params.extend([session_id, export_id])
        with self.db.tx() as conn:
            conn.execute(
                f"UPDATE export_jobs SET {sets} WHERE session_id = ? AND id = ?",
                params,
            )
        return self.get(session_id, export_id)  # type: ignore[return-value]

    # ------------------------------------------------------------- execution
    def run(self, session_id: str, export_id: str) -> ExportJob:
        job = self.get(session_id, export_id)
        if job is None:
            raise ExportError(f"export job {export_id} not found")
        if job.status == "succeeded":
            return job
        job = self._set_status(session_id, export_id, status="running")
        try:
            payload = self._build_bundle(session_id)
            out_dir = self.session_dir(session_id)
            if job.format == "json":
                out_path = out_dir / f"{export_id}.json"
                atomic_write_text(out_path, json.dumps(payload, indent=2, ensure_ascii=False))
            else:
                out_path = out_dir / f"{export_id}.tar.gz"
                self._write_tar(out_path, payload)
            return self._set_status(
                session_id, export_id, status="succeeded", result_path=str(out_path)
            )
        except Exception as exc:
            return self._set_status(session_id, export_id, status="failed", error=str(exc))

    def requeue_interrupted(self) -> int:
        """Boot recovery: exports left running/pending by a dead process.

        They are NOT auto-run here (bundle content may be stale); marking them
        failed with a clear reason lets the owner simply re-export.
        """
        now = iso_now()
        with self.db.tx() as conn:
            cur = conn.execute(
                """
                UPDATE export_jobs
                SET status = 'failed', error = 'interrupted by application restart; re-run the export',
                    updated_at = ?, finished_at = ?
                WHERE status IN ('pending', 'running')
                """,
                (now, now),
            )
            return cur.rowcount

    def _build_bundle(self, session_id: str) -> dict[str, Any]:
        reports = self.reports.list(session_id)
        report_bodies = {}
        for r in reports:
            try:
                report_bodies[r.id] = self.reports.read_markdown(r)
            except Exception:
                report_bodies[r.id] = ""
        payload: dict[str, Any] = {
            "session_id": session_id,
            "exported_at": iso_now(),
            "opportunities": [o.to_dict() for o in self.opportunities.list(session_id)],
            "candidates": [c.to_dict() for c in self.candidates.list(session_id)],
            "reports": [r.to_dict() for r in reports],
            "report_bodies": report_bodies,
        }
        if self.observations is not None:
            payload["observations"] = [
                o.to_dict() for o in self.observations.list(session_id, limit=5000)
            ]
        if self.evidence is not None:
            payload["evidence"] = [
                e.to_dict() for e in self.evidence.list(session_id, limit=5000)
            ]
        if self.events is not None:
            payload["events"] = [e.to_dict() for e in self.events.tail(session_id, limit=5000)]
        if self.checkpoints is not None:
            payload["checkpoints"] = [c.to_dict() for c in self.checkpoints.list(session_id)]
        # Defensive: exports must never carry credential-shaped material.
        return redact(payload)

    def _write_tar(self, out_path: Path, payload: dict[str, Any]) -> None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            data = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
            info = tarfile.TarInfo(name="export.json")
            info.size = len(data)
            info.mtime = int(_time.time())
            tar.addfile(info, io.BytesIO(data))
        atomic_write_bytes(out_path, buf.getvalue())

    def _row(self, row: sqlite3.Row) -> ExportJob:
        return ExportJob(
            id=row["id"],
            session_id=row["session_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            status=row["status"],
            format=row["format"],
            error=row["error"],
            result_path=row["result_path"],
            attempts=row["attempts"] if "attempts" in row.keys() else 0,
            started_at=row["started_at"] if "started_at" in row.keys() else None,
            finished_at=row["finished_at"] if "finished_at" in row.keys() else None,
        )
