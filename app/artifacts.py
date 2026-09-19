"""Artifact store.

Binary/large research artifacts (screenshots, page snapshots, PDFs, recordings,
generated files) are stored on the persistent filesystem under a per-session
directory and registered in the `artifacts` table. Writes are atomic so a
crash never leaves a half-written artifact.
"""
from __future__ import annotations

import json
import mimetypes
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import Config
from app.db import Database, atomic_write_bytes, atomic_write_text
from app.timeutil import iso_now

ARTIFACT_KINDS = (
    "screenshot",
    "page_snapshot",
    "pdf",
    "recording",
    "data_export",
    "model_output",
    "other",
)


class ArtifactError(Exception):
    pass


@dataclass
class Artifact:
    id: str
    session_id: str
    created_at: str
    kind: str
    path: str
    content_type: str
    size_bytes: int
    meta: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "kind": self.kind,
            "path": self.path,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
            "meta": self.meta,
        }


class ArtifactStore:
    def __init__(self, db: Database, config: Config) -> None:
        self.db = db
        self.config = config

    # ------------------------------------------------------------------ paths
    def session_dir(self, session_id: str) -> Path:
        return self.config.artifacts_dir / session_id

    # ------------------------------------------------------------------ write
    def save_bytes(
        self,
        session_id: str,
        *,
        kind: str,
        filename: str,
        data: bytes,
        meta: dict[str, Any] | None = None,
    ) -> Artifact:
        if kind not in ARTIFACT_KINDS:
            raise ArtifactError(f"invalid artifact kind {kind!r}")
        path = self.session_dir(session_id) / filename
        atomic_write_bytes(path, data)
        return self._register(session_id, kind=kind, path=path, meta=meta)

    def save_text(
        self,
        session_id: str,
        *,
        kind: str,
        filename: str,
        text: str,
        meta: dict[str, Any] | None = None,
    ) -> Artifact:
        if kind not in ARTIFACT_KINDS:
            raise ArtifactError(f"invalid artifact kind {kind!r}")
        path = self.session_dir(session_id) / filename
        atomic_write_text(path, text)
        return self._register(session_id, kind=kind, path=path, meta=meta)

    def register_external_file(
        self,
        session_id: str,
        *,
        kind: str,
        path: Path,
        meta: dict[str, Any] | None = None,
    ) -> Artifact:
        """Register a file already written under the session's artifact dir."""
        if kind not in ARTIFACT_KINDS:
            raise ArtifactError(f"invalid artifact kind {kind!r}")
        resolved = path.resolve()
        base = self.session_dir(session_id).resolve()
        if base not in resolved.parents:
            raise ArtifactError("external artifact path escapes session directory")
        return self._register(session_id, kind=kind, path=resolved, meta=meta)

    def _register(
        self,
        session_id: str,
        *,
        kind: str,
        path: Path,
        meta: dict[str, Any] | None = None,
    ) -> Artifact:
        size = path.stat().st_size
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        art_id = uuid.uuid4().hex
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO artifacts (id, session_id, created_at, kind, path, content_type, size_bytes, meta) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (art_id, session_id, iso_now(), kind, str(path), ctype, size, json.dumps(meta or {})),
            )
        return Artifact(
            id=art_id,
            session_id=session_id,
            created_at=iso_now(),
            kind=kind,
            path=str(path),
            content_type=ctype,
            size_bytes=size,
            meta=meta or {},
        )

    # ------------------------------------------------------------------- read
    def get(self, session_id: str, artifact_id: str) -> Artifact | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE session_id = ? AND id = ?",
                (session_id, artifact_id),
            ).fetchone()
        return self._row(row) if row else None

    def require(self, session_id: str, artifact_id: str) -> Artifact:
        a = self.get(session_id, artifact_id)
        if a is None:
            raise ArtifactError(f"artifact {artifact_id} not found in session {session_id}")
        return a

    def list(self, session_id: str, *, kind: str | None = None, limit: int = 500) -> list[Artifact]:
        q = "SELECT * FROM artifacts WHERE session_id = ?"
        params: list[Any] = [session_id]
        if kind is not None:
            q += " AND kind = ?"
            params.append(kind)
        q += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.db.read() as conn:
            rows = conn.execute(q, params).fetchall()
        return [self._row(r) for r in rows]

    def read_bytes(self, artifact: Artifact) -> bytes:
        path = Path(artifact.path)
        if not path.is_file():
            raise ArtifactError(f"artifact file missing on disk: {artifact.path}")
        return path.read_bytes()

    def _row(self, row: sqlite3.Row) -> Artifact:
        return Artifact(
            id=row["id"],
            session_id=row["session_id"],
            created_at=row["created_at"],
            kind=row["kind"],
            path=row["path"],
            content_type=row["content_type"],
            size_bytes=row["size_bytes"],
            meta=json.loads(row["meta"]),
        )
