"""Reference image uploads.

The owner can attach reference images (book covers, market screenshots,
KDSpy captures) to a session. Images are validated, stored atomically under
the session's upload namespace, and registered in the `uploads` table.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import Config
from app.db import Database, atomic_write_bytes
from app.timeutil import iso_now

ALLOWED_CONTENT_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB


class UploadError(Exception):
    pass


@dataclass
class Upload:
    id: str
    session_id: str
    created_at: str
    filename: str
    stored_path: str
    content_type: str
    size_bytes: int
    meta: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "filename": self.filename,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
            "meta": self.meta,
        }


class UploadStore:
    def __init__(self, db: Database, config: Config) -> None:
        self.db = db
        self.config = config

    def session_dir(self, session_id: str) -> Path:
        return self.config.uploads_dir / session_id

    def save(
        self,
        session_id: str,
        *,
        filename: str,
        content_type: str,
        data: bytes,
        meta: dict[str, Any] | None = None,
    ) -> Upload:
        # Validate session exists (isolation check).
        ext = ALLOWED_CONTENT_TYPES.get(content_type)
        if ext is None:
            raise UploadError(f"unsupported image content type {content_type!r}")
        if len(data) == 0:
            raise UploadError("empty upload")
        if len(data) > MAX_UPLOAD_BYTES:
            raise UploadError(f"upload exceeds {MAX_UPLOAD_BYTES} bytes")
        safe_name = Path(filename or f"upload{ext}").name
        stored = self.session_dir(session_id) / f"{uuid.uuid4().hex}{ext}"
        atomic_write_bytes(stored, data)
        up_id = uuid.uuid4().hex
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO uploads (id, session_id, created_at, filename, stored_path, content_type, size_bytes, meta) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (up_id, session_id, iso_now(), safe_name, str(stored), content_type, len(data), json.dumps(meta or {})),
            )
        return Upload(
            id=up_id,
            session_id=session_id,
            created_at=iso_now(),
            filename=safe_name,
            stored_path=str(stored),
            content_type=content_type,
            size_bytes=len(data),
            meta=meta or {},
        )

    def get(self, session_id: str, upload_id: str) -> Upload | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM uploads WHERE session_id = ? AND id = ?",
                (session_id, upload_id),
            ).fetchone()
        return self._row(row) if row else None

    def list(self, session_id: str) -> list[Upload]:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM uploads WHERE session_id = ? ORDER BY created_at ASC",
                (session_id,),
            ).fetchall()
        return [self._row(r) for r in rows]

    def read_bytes(self, upload: Upload) -> bytes:
        path = Path(upload.stored_path)
        if not path.is_file():
            raise UploadError(f"upload file missing on disk: {upload.stored_path}")
        return path.read_bytes()

    def _row(self, row: sqlite3.Row) -> Upload:
        return Upload(
            id=row["id"],
            session_id=row["session_id"],
            created_at=row["created_at"],
            filename=row["filename"],
            stored_path=row["stored_path"],
            content_type=row["content_type"],
            size_bytes=row["size_bytes"],
            meta=json.loads(row["meta"]),
        )
