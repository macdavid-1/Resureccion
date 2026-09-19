"""Browser live view: optional, bounded screenshot streaming.

The owner may watch the research browser live from their phone. The live
view is strictly OPTIONAL: research runs identically when nobody is
watching, and frames are only captured while a watcher is connected (plus a
low-frequency idle frame so the page can render something immediately).

Storage is a bounded ring in SQLite (live_view_frames): at most
`max_frames` frames per session, oldest pruned on insert. Frames are
JPEG-compressed, downscaled, and capped in size — a 2-core/16GB box must
never balloon memory because someone left the live view open.

Privacy: frames may contain page content; they are owner-only (auth
required on the stream endpoint) and never enter exports or reports.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from app.db import Database
from app.timeutil import iso_now


@dataclass
class LiveFrame:
    id: int
    session_id: str
    created_at: str
    kind: str
    url: str
    title: str
    data: bytes

    def meta_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "kind": self.kind,
            "url": self.url,
            "title": self.title,
            "bytes": len(self.data),
        }


class LiveViewStore:
    """Bounded per-session frame ring with pruning."""

    def __init__(self, db: Database, *, max_frames: int = 12, max_frame_bytes: int = 220_000) -> None:
        self.db = db
        self.max_frames = max_frames
        self.max_frame_bytes = max_frame_bytes

    def put(
        self,
        session_id: str,
        data: bytes,
        *,
        kind: str = "screenshot",
        url: str = "",
        title: str = "",
    ) -> LiveFrame | None:
        """Store one frame and prune the ring. Returns None for oversized or
        empty frames (never store junk)."""
        if not data or len(data) > self.max_frame_bytes:
            return None
        now = iso_now()
        with self.db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO live_view_frames (session_id, created_at, kind, data, url, title) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, now, kind, data, url[:500], title[:200]),
            )
            frame_id = int(cur.lastrowid)
            conn.execute(
                """
                DELETE FROM live_view_frames
                WHERE session_id = ? AND id NOT IN (
                    SELECT id FROM live_view_frames WHERE session_id = ?
                    ORDER BY id DESC LIMIT ?
                )
                """,
                (session_id, session_id, self.max_frames),
            )
        return LiveFrame(frame_id, session_id, now, kind, url[:500], title[:200], data)

    def latest(self, session_id: str) -> LiveFrame | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM live_view_frames WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return self._row(row) if row else None

    def latest_meta(self, session_id: str) -> dict[str, Any] | None:
        f = self.latest(session_id)
        return f.meta_dict() if f else None

    def count(self, session_id: str) -> int:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM live_view_frames WHERE session_id = ?", (session_id,)
            ).fetchone()
        return int(row["n"]) if row else 0

    def clear(self, session_id: str) -> None:
        with self.db.tx() as conn:
            conn.execute("DELETE FROM live_view_frames WHERE session_id = ?", (session_id,))

    def _row(self, row: sqlite3.Row) -> LiveFrame:
        return LiveFrame(
            id=row["id"],
            session_id=row["session_id"],
            created_at=row["created_at"],
            kind=row["kind"],
            url=row["url"],
            title=row["title"],
            data=row["data"],
        )


def downscale_jpeg(png_bytes: bytes, *, max_width: int = 900, quality: int = 62) -> bytes | None:
    """Compress a PNG screenshot to a bounded JPEG for streaming.

    Returns None when PIL is unavailable or input is not an image — the live
    view is optional and must never break research.
    """
    if not png_bytes:
        return None
    try:
        import io

        from PIL import Image

        img = Image.open(io.BytesIO(png_bytes))
        if img.width > max_width:
            ratio = max_width / img.width
            img = img.resize((max_width, max(1, int(img.height * ratio))))
        out = io.BytesIO()
        img.convert("RGB").save(out, format="JPEG", quality=quality)
        return out.getvalue()
    except Exception:
        return None
