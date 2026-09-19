"""Research reports.

The final (and intermediate) research reports. The report body is written
atomically to the persistent filesystem; the `reports` table holds metadata
so reports can be listed and re-fetched without parsing the filesystem.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import Config
from app.db import Database, atomic_write_text
from app.timeutil import iso_now

REPORT_KINDS = ("intermediate", "final", "niche_deep_dive", "postmortem")


class ReportError(Exception):
    pass


@dataclass
class Report:
    id: str
    session_id: str
    created_at: str
    kind: str
    title: str
    body_path: str
    meta: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "kind": self.kind,
            "title": self.title,
            "meta": self.meta,
        }


class ReportStore:
    def __init__(self, db: Database, config: Config) -> None:
        self.db = db
        self.config = config

    def session_dir(self, session_id: str) -> Path:
        return self.config.reports_dir / session_id

    def save(
        self,
        session_id: str,
        *,
        kind: str,
        title: str,
        body_markdown: str,
        meta: dict[str, Any] | None = None,
    ) -> Report:
        if kind not in REPORT_KINDS:
            raise ReportError(f"invalid report kind {kind!r}")
        if not title.strip():
            raise ReportError("report title must be non-empty")
        slug = "".join(c if c.isalnum() else "-" for c in title.lower()).strip("-")[:80] or "report"
        path = self.session_dir(session_id) / f"{uuid.uuid4().hex[:8]}-{slug}.md"
        atomic_write_text(path, body_markdown)
        rep_id = uuid.uuid4().hex
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO reports (id, session_id, created_at, kind, title, body_path, meta) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (rep_id, session_id, iso_now(), kind, title, str(path), json.dumps(meta or {})),
            )
        return Report(
            id=rep_id,
            session_id=session_id,
            created_at=iso_now(),
            kind=kind,
            title=title,
            body_path=str(path),
            meta=meta or {},
        )

    def get(self, session_id: str, report_id: str) -> Report | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM reports WHERE session_id = ? AND id = ?",
                (session_id, report_id),
            ).fetchone()
        return self._row(row) if row else None

    def latest(self, session_id: str, *, kind: str | None = None) -> Report | None:
        q = "SELECT * FROM reports WHERE session_id = ?"
        params: list[Any] = [session_id]
        if kind is not None:
            q += " AND kind = ?"
            params.append(kind)
        q += " ORDER BY created_at DESC LIMIT 1"
        with self.db.read() as conn:
            row = conn.execute(q, params).fetchone()
        return self._row(row) if row else None

    def list(self, session_id: str) -> list[Report]:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM reports WHERE session_id = ? ORDER BY created_at DESC",
                (session_id,),
            ).fetchall()
        return [self._row(r) for r in rows]

    def read_markdown(self, report: Report) -> str:
        path = Path(report.body_path)
        if not path.is_file():
            raise ReportError(f"report file missing on disk: {report.body_path}")
        return path.read_text(encoding="utf-8")

    def save_model_json(
        self,
        session_id: str,
        *,
        report: Report,
        model_json: str,
    ) -> Path:
        """Persist the structured report model next to the rendered body.

        The report stays editable/re-renderable from structured data: the
        model is the source of truth; markdown is a rendering.
        """
        path = Path(report.body_path).with_suffix(".model.json")
        atomic_write_text(path, model_json)
        return path

    def read_model_json(self, report: Report) -> dict[str, Any] | None:
        path = Path(report.body_path).with_suffix(".model.json")
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _row(self, row: sqlite3.Row) -> Report:
        return Report(
            id=row["id"],
            session_id=row["session_id"],
            created_at=row["created_at"],
            kind=row["kind"],
            title=row["title"],
            body_path=row["body_path"],
            meta=json.loads(row["meta"]),
        )
