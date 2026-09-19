"""Session recording: the live research view as a downloadable artifact.

The owner presses Record; the SERVER captures the live browser frames at a
bounded cadence and encodes them — together with the synchronized
operational research trace (the same concise rationale lines the live UI
shows; NEVER model chain-of-thought) — into a single downloadable artifact.
The owner's connectivity only affects WATCHING the live view; recording
itself is fully server-side and continues while the phone is offline.

Durability contract (the reason this module exists):
- Frames are streamed to DISK as they are captured (one JPEG per frame in a
  per-recording capture directory) — RAM stays flat for recordings of any
  length; a 2-core/16GB box is never ballooned by a 6-hour recording.
- Recording state is persisted in the `recordings` table. If the container
  restarts mid-recording, boot recovery finalizes whatever was already
  captured on disk into a real artifact instead of silently losing hours.
- Encoding tries WebM (imageio-ffmpeg) incrementally from disk, burning a
  research HUD (elapsed REC timecode + current activity line) into every
  frame — the video itself carries the synchronized operational trace.
  Graceful fallback: a self-contained annotated HTML bundle (sampled frames
  + synchronized trace timeline). Exactly one artifact per recording.
- Recording must never break research: every failure is contained.
"""
from __future__ import annotations

import asyncio
import io
import json
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.artifacts import ArtifactStore
from app.config import Config
from app.db import Database
from app.live_view import LiveViewStore
from app.timeutil import iso_now
from app.trace import ActivityTrace

_CAPTURE_INTERVAL = 0.5        # 2 fps max
_MAX_FRAMES = 4 * 60 * 60 * 2  # hard ceiling: ~2h at 2fps; disk-bound after
_FINALIZE_SAMPLE = 240         # frames embedded in the HTML bundle


@dataclass
class RecordingState:
    session_id: str
    recording_id: str
    started_at: str
    frame_count: int = 0
    artifact_id: str | None = None
    stop_requested: bool = False
    task: asyncio.Task | None = None
    last_error: str | None = None
    status: str = "recording"

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "recording_id": self.recording_id,
            "started_at": self.started_at,
            "frame_count": self.frame_count,
            "artifact_id": self.artifact_id,
            "stop_requested": self.stop_requested,
            "active": self.active,
            "status": self.status,
            "last_error": self.last_error,
        }

    @property
    def active(self) -> bool:
        return (
            self.status == "recording"
            and not self.stop_requested
            and (self.task is None or not self.task.done())
        )


class SessionRecorder:
    """One recorder per process; at most one live recording per session."""

    def __init__(
        self,
        db: Database,
        config: Config,
        *,
        live_view: LiveViewStore,
        artifacts: ArtifactStore,
        trace: ActivityTrace,
        browser_manager: Any,
    ) -> None:
        self.db = db
        self.config = config
        self.live_view = live_view
        self.artifacts = artifacts
        self.trace = trace
        self.browser = browser_manager
        self._recordings: dict[str, RecordingState] = {}

    # ------------------------------------------------------------------ paths
    def capture_dir(self, recording_id: str) -> Path:
        return self.config.data_dir / "recordings" / recording_id

    # ------------------------------------------------------------- persistence
    def _insert_row(self, rec: RecordingState) -> None:
        with self.db.tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO recordings (id, session_id, started_at, status) "
                "VALUES (?, ?, ?, 'recording')",
                (rec.recording_id, rec.session_id, rec.started_at),
            )

    def _update_row(
        self,
        recording_id: str,
        *,
        status: str,
        artifact_id: str | None = None,
        stop_requested_at: str | None = None,
        last_error: str | None = None,
    ) -> None:
        sets = ["status = ?"]
        params: list[Any] = [status]
        if artifact_id is not None:
            sets.append("artifact_id = ?")
            params.append(artifact_id)
        if stop_requested_at is not None:
            sets.append("stop_requested_at = ?")
            params.append(stop_requested_at)
        if last_error is not None:
            sets.append("last_error = ?")
            params.append(last_error)
        params.append(recording_id)
        with self.db.tx() as conn:
            conn.execute(f"UPDATE recordings SET {', '.join(sets)} WHERE id = ?", params)

    def _row(self, recording_id: str) -> dict[str, Any] | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM recordings WHERE id = ?", (recording_id,)
            ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------ API
    def status(self, session_id: str) -> dict[str, Any] | None:
        """Live state if recording in this process, else the durable row."""
        rec = self._recordings.get(session_id)
        if rec is not None:
            return rec.to_dict()
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM recordings WHERE session_id = ? ORDER BY started_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["active"] = d["status"] == "recording"
        return d

    def is_recording(self, session_id: str) -> bool:
        rec = self._recordings.get(session_id)
        return bool(rec and rec.active)

    def start(self, session_id: str) -> dict[str, Any]:
        if self.is_recording(session_id):
            return self._recordings[session_id].to_dict()
        rec = RecordingState(
            session_id=session_id,
            recording_id=uuid.uuid4().hex,
            started_at=iso_now(),
        )
        self._insert_row(rec)
        try:
            cap_dir = self.capture_dir(rec.recording_id)
            cap_dir.mkdir(parents=True, exist_ok=True)
            # Common time base for frames + trace, persisted so a recovered
            # recording finalizes with the same synchronized timeline.
            (cap_dir / "meta.json").write_text(
                json.dumps({"started_epoch": time.time(), "started_at": rec.started_at})
            )
        except Exception:
            pass  # capture loop degrades to the live-ring fallback
        rec.task = asyncio.create_task(self._run(rec))
        self._recordings[session_id] = rec
        self.trace.record(
            session_id, "report",
            "Recording started: the live research view is being captured server-side to a session artifact",
        )
        return rec.to_dict()

    async def stop(self, session_id: str) -> dict[str, Any] | None:
        rec = self._recordings.get(session_id)
        if rec is None:
            return None
        if not rec.stop_requested:
            rec.stop_requested = True
            self._update_row(
                rec.recording_id, status=rec.status,
                stop_requested_at=iso_now(),
            )
        task = rec.task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=15.0)
            except Exception:
                pass  # long encode: finalize finishes in the background
        self._recordings.pop(session_id, None)
        row = self._row(rec.recording_id)
        return rec.to_dict() | ({"artifact_id": row["artifact_id"]} if row else {})

    # ------------------------------------------------------- boot recovery
    def recover_on_boot(self, *, wait: bool = True) -> list[str]:
        """Finalize recordings orphaned by a restart.

        Anything the DB says is `recording` but no process owns is finished
        from the frames already on disk. Recording survives container
        restarts; hours of footage are never silently lost. With wait=False
        the (possibly long) encode runs in a background thread so app boot
        is never blocked; wait=True completes inline (tests, tooling).
        """
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM recordings WHERE status = 'recording'"
            ).fetchall()
        recovered: list[str] = []

        def finish(row: Any) -> str | None:
            rec = RecordingState(
                session_id=row["session_id"],
                recording_id=row["id"],
                started_at=row["started_at"],
                stop_requested=True,
                status="recording",
            )
            try:
                self._update_row(rec.recording_id, status="finalizing")
                artifact_id = self._finalize_from_disk(
                    rec, interrupt_reason="interrupted by restart"
                )
                self._update_row(
                    rec.recording_id, status="completed",
                    artifact_id=artifact_id, last_error=None,
                )
                if artifact_id:
                    self.trace.record(
                        rec.session_id, "recovery",
                        "A recording interrupted by a restart was recovered and saved to session artifacts",
                    )
                return rec.recording_id
            except Exception as exc:
                self._update_row(
                    rec.recording_id, status="failed",
                    last_error=str(exc)[:300],
                )
                return None

        for row in rows:
            if wait:
                rid = finish(row)
                if rid:
                    recovered.append(rid)
            else:
                self._update_row(row["id"], status="finalizing")
                threading.Thread(target=finish, args=(row,), daemon=True).start()
                recovered.append(row["id"])
        return recovered

    # ------------------------------------------------------------------ loop
    async def _run(self, rec: RecordingState) -> None:
        """Capture frames + trace to DISK until stopped; then finalize."""
        cap_dir = self.capture_dir(rec.recording_id)
        trace_after = 0
        loop = asyncio.get_event_loop()
        next_capture = 0.0
        try:
            while not rec.stop_requested and rec.frame_count < _MAX_FRAMES:
                now = loop.time()
                if now >= next_capture:
                    png = await self._safe_live_frame(rec.session_id)
                    if png is None:
                        frame = self.live_view.latest(rec.session_id)
                        png = frame.data if frame else None
                    if png:
                        self._write_frame(cap_dir, rec.frame_count, png)
                        rec.frame_count += 1
                    next_capture = now + _CAPTURE_INTERVAL
                # Pull new trace lines so the recording stays synchronized.
                new_entries = self.trace.recent(rec.session_id, after_id=trace_after, limit=50)
                if new_entries:
                    self._append_trace(cap_dir, new_entries)
                    trace_after = new_entries[-1].id
                await asyncio.sleep(min(_CAPTURE_INTERVAL, 0.25))
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # never break research
            rec.last_error = str(exc)[:300]
        finally:
            rec.status = "finalizing"
            try:
                # Encoding hours of frames must never block the event loop
                # (SSE, heartbeats, API) — offload to a worker thread.
                artifact_id = await asyncio.get_event_loop().run_in_executor(
                    None, self._finalize_from_disk, rec
                )
                rec.artifact_id = artifact_id
                rec.status = "completed"
                self._update_row(
                    rec.recording_id, status="completed",
                    artifact_id=artifact_id, last_error=rec.last_error,
                )
            except Exception as exc:
                rec.status = "failed"
                self._update_row(
                    rec.recording_id, status="failed",
                    last_error=str(exc)[:300],
                )

    def _write_frame(self, cap_dir: Path, index: int, data: bytes) -> None:
        try:
            (cap_dir / f"f{index:08d}.img").write_bytes(data)
        except Exception:
            pass  # a dropped frame must not kill the recording

    def _append_trace(self, cap_dir: Path, entries: list[Any]) -> None:
        try:
            with (cap_dir / "trace.jsonl").open("a", encoding="utf-8") as fh:
                for e in entries:
                    fh.write(json.dumps({
                        "t": time.time(), "kind": e.kind, "text": e.text, "at": e.created_at,
                    }, ensure_ascii=False) + "\n")
        except Exception:
            pass  # trace loss must not kill the recording

    async def _safe_live_frame(self, session_id: str) -> bytes | None:
        try:
            return await self.browser.live_screenshot(session_id)
        except Exception:
            return None

    # ---------------------------------------------------------------- finalize
    def _finalize_from_disk(
        self, rec: RecordingState, *, interrupt_reason: str | None = None
    ) -> str | None:
        """Encode the on-disk capture into exactly one artifact.

        WebM (imageio-ffmpeg) when available — encoded incrementally from
        disk so RAM stays flat. Fallback: self-contained annotated HTML
        bundle (sampled frames + synchronized trace). Both are downloadable.
        """
        cap_dir = self.capture_dir(rec.recording_id)
        frame_files = sorted(cap_dir.glob("f*.img")) if cap_dir.is_dir() else []
        trace_file = cap_dir / "trace.jsonl"
        started_epoch = self._started_epoch(cap_dir)
        if not frame_files and not trace_file.exists():
            self.trace.record(
                rec.session_id, "report",
                "Recording ended: nothing was captured (browser idle); no artifact produced",
            )
            self._cleanup(cap_dir)
            return None
        entries = self._read_trace(trace_file)
        stamp = rec.started_at.replace(":", "").replace("-", "")[:15]
        webm_id = self._try_webm_from_disk(
            rec, frame_files, stamp, entries, started_epoch
        )
        if webm_id:
            self.trace.record(
                rec.session_id, "report",
                f"Recording complete: {len(frame_files)} frames + burned-in research timeline saved as video"
                + (f" ({interrupt_reason})" if interrupt_reason else ""),
            )
            self._cleanup(cap_dir)
            return webm_id
        html_id = self._html_bundle_from_disk(
            rec, frame_files, entries, stamp, interrupt_reason, started_epoch
        )
        self.trace.record(
            rec.session_id, "report",
            f"Recording complete: {len(frame_files)} frames + synchronized trace saved as an interactive artifact"
            + (f" ({interrupt_reason})" if interrupt_reason else ""),
        )
        self._cleanup(cap_dir)
        return html_id

    def _started_epoch(self, cap_dir: Path) -> float:
        try:
            meta = json.loads((cap_dir / "meta.json").read_text())
            return float(meta.get("started_epoch", 0.0))
        except Exception:
            return 0.0

    def _read_trace(self, trace_file: Path) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        try:
            with trace_file.open("r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        entries.append(json.loads(line))
                    except Exception:
                        continue
        except Exception:
            pass
        return entries

    def _cleanup(self, cap_dir: Path) -> None:
        try:
            for f in cap_dir.iterdir():
                f.unlink(missing_ok=True)
            cap_dir.rmdir()
        except Exception:
            pass

    def _try_webm_from_disk(
        self,
        rec: RecordingState,
        frame_files: list[Path],
        stamp: str,
        entries: list[dict[str, Any]],
        started_epoch: float,
    ) -> str | None:
        """Encode frames into a WebM using imageio-ffmpeg if present.

        A research HUD (REC timecode + current activity line) is burned into
        every frame, so the video itself carries the synchronized trace —
        like a proper screen recording with a status bar. Frames are read
        and rendered one at a time — memory stays flat.
        """
        if not frame_files:
            return None
        try:
            import tempfile

            import imageio  # type: ignore
            import numpy as np  # type: ignore
            from PIL import Image  # type: ignore

            ordered = sorted(entries, key=lambda e: e.get("t", 0.0))
            out_path = tempfile.mktemp(suffix=".webm")
            with imageio.get_writer(
                out_path, format="ffmpeg", fps=2,
                codec="libvpx", quality=6, macro_block_size=8,
            ) as w:
                ti = 0
                for i, f in enumerate(frame_files):
                    try:
                        img = Image.open(io.BytesIO(f.read_bytes())).convert("RGB")
                    except Exception:
                        continue
                    if img.width > 800:
                        r = 800 / img.width
                        img = img.resize((800, max(1, int(img.height * r))))
                    t_frame = started_epoch + i * _CAPTURE_INTERVAL
                    while (
                        ti + 1 < len(ordered)
                        and ordered[ti + 1].get("t", 0.0) <= t_frame
                    ):
                        ti += 1
                    entry = (
                        ordered[ti]
                        if ordered and ordered[ti].get("t", 0.0) <= t_frame
                        else None
                    )
                    _burn_hud(img, i * _CAPTURE_INTERVAL, entry)
                    w.append_data(np.asarray(img))
            data = Path(out_path).read_bytes()
            if not data:
                return None
            art = self.artifacts.save_bytes(
                rec.session_id, kind="recording",
                filename=f"recording-{stamp}.webm", data=data,
                meta={"frames": len(frame_files), "format": "webm",
                      "hud": True, "started_at": rec.started_at},
            )
            Path(out_path).unlink(missing_ok=True)
            return art.id
        except Exception:
            return None

    def _html_bundle_from_disk(
        self,
        rec: RecordingState,
        frame_files: list[Path],
        entries: list[dict[str, Any]],
        stamp: str,
        interrupt_reason: str | None,
        started_epoch: float,
    ) -> str | None:
        """Self-contained HTML player: sampled frames + synchronized trace.

        Bounded: at most `_FINALIZE_SAMPLE` frames embedded (sampled evenly)
        so the artifact stays a few MB even for long sessions. Frames are
        read one at a time; memory stays flat.
        """
        import base64

        frames: list[tuple[float, bytes]] = []
        total = len(frame_files)
        if total:
            step = max(1, total // _FINALIZE_SAMPLE)
            for i in range(0, total, step):
                try:
                    # Frame n was captured ~started_epoch + n * interval.
                    frames.append((started_epoch + i * _CAPTURE_INTERVAL, frame_files[i].read_bytes()))
                except Exception:
                    continue
        if len(entries) > 400:
            step = len(entries) / 400
            entries = [entries[int(i * step)] for i in range(400)]
        if not frames and not entries:
            return None
        t0 = started_epoch or (
            frames[0][0] if frames else (entries[0].get("t", 0.0) if entries else 0.0)
        )
        payload = {
            "t0": t0,
            "frames": [
                {"t": round(t - t0, 2), "jpeg": _b64_image(f)}
                for t, f in frames
            ],
            "trace": [
                {"t": round(e.get("t", 0.0) - t0, 2), "kind": e.get("kind", ""),
                 "text": e.get("text", ""), "at": e.get("at", "")}
                for e in entries
            ],
            "meta": {
                "started_at": rec.started_at, "session_id": rec.session_id,
                **({"note": interrupt_reason} if interrupt_reason else {}),
            },
        }
        html = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Resurrección recording</title><style>"
            "body{background:#000;color:#e8eaf2;font-family:system-ui,sans-serif;margin:0;padding:16px}"
            "#stage{max-width:800px;margin:0 auto}img{width:100%;border-radius:6px;border:1px solid #1c2340}"
            "#line{font-size:14px;color:#aab3d0;padding:10px 2px;min-height:44px}"
            "#tl{color:#5d68f0;font-size:12px;font-variant-numeric:tabular-nums}"
            "</style></head><body><div id='stage'>"
            "<div id='tl'>00:00</div><img id='f' alt='frame'><div id='line'></div>"
            "<div id='list'></div></div><script>const D="
            + json.dumps(payload, ensure_ascii=False)
            + ";let i=0;const f=document.getElementById('f'),l=document.getElementById('line'),"
            "tl=document.getElementById('tl'),list=document.getElementById('list');"
            "const fmt=s=>String(Math.floor(s/60)).padStart(2,'0')+':'+String(Math.floor(s%60)).padStart(2,'0');"
            "function tick(){if(!D.frames.length)return;const fr=D.frames[i%D.frames.length];"
            "f.src=fr.jpeg;tl.textContent=fmt(fr.t);"
            "const near=D.trace.filter(e=>e.t<=fr.t).slice(-1)[0];"
            "l.textContent=near?near.text:'';i++;setTimeout(tick,500);}"
            "D.trace.slice().reverse().forEach(e=>{const d=document.createElement('div');"
            "d.style.cssText='padding:6px 0;border-bottom:1px solid #141a30;color:#c7cde6;font-size:13px';"
            "d.textContent='['+fmt(e.t)+'] '+e.text;list.appendChild(d);});"
            "if(D.frames.length){tick();}else{l.textContent='(no frames captured)';}"
            "</script></body></html>"
        )
        try:
            art = self.artifacts.save_text(
                rec.session_id, kind="recording",
                filename=f"recording-{stamp}.html", text=html,
                meta={"frames": len(frames), "format": "html_bundle",
                      "total_frames": total, "trace_entries": len(entries),
                      "started_at": rec.started_at,
                      **({"note": interrupt_reason} if interrupt_reason else {})},
            )
            return art.id
        except Exception:
            return None


def _b64_image(data: bytes) -> str:
    """data: URI with correct mime for PNG or JPEG frame bytes."""
    import base64

    mime = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


# ------------------------------------------------------------------- HUD

def _fmt_tc(seconds: float) -> str:
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


_HUD_FONTS: dict[int, Any] = {}


def _hud_font(size: int) -> Any:
    """Best-effort TTF (DejaVu/Liberation), else Pillow's scalable default."""
    if size in _HUD_FONTS:
        return _HUD_FONTS[size]
    from PIL import ImageFont

    font = None
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ):
        try:
            font = ImageFont.truetype(path, size)
            break
        except Exception:
            continue
    if font is None:
        try:
            font = ImageFont.load_default(size=size)
        except TypeError:
            font = ImageFont.load_default()
    _HUD_FONTS[size] = font
    return font


def _burn_hud(img: Any, elapsed: float, entry: dict[str, Any] | None) -> None:
    """Burn the recording HUD into a frame (in place): a dark bottom bar
    with the elapsed REC timecode and the current activity trace line —
    the same concise operational text the live UI shows (never
    chain-of-thought), synchronized by capture time."""
    from PIL import ImageDraw

    w, h = img.size
    bar = max(46, h // 6)
    draw = ImageDraw.Draw(img, "RGBA")
    draw.rectangle([0, h - bar, w, h], fill=(3, 5, 14, 214))
    draw.rectangle([0, h - bar, w, h - bar + 3], fill=(61, 43, 255, 255))
    pad = 10
    draw.text(
        (pad, h - bar + 9), f"REC {_fmt_tc(elapsed)}",
        font=_hud_font(13), fill=(226, 231, 246, 255),
    )
    if entry:
        kind = (entry.get("kind") or "").strip().upper()
        text = (entry.get("text") or "").strip()
        f13 = _hud_font(13)
        x = pad
        if kind:
            draw.text(
                (x, h - bar + 27), kind,
                font=_hud_font(11), fill=(126, 112, 255, 255),
            )
            x += draw.textlength(kind + "   ", font=f13) + 4
        maxw = w - x - pad
        while text and draw.textlength(text, font=f13) > maxw:
            text = text[:-2].rstrip()
        if text:
            draw.text((x, h - bar + 27), text, font=f13, fill=(240, 243, 251, 255))
