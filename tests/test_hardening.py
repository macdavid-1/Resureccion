"""Production-hardening tests: the system must fail gracefully.

Covers the audit checklist: corrupted checkpoints, malformed JSON payloads,
duplicate events, and storage-level failures never crash recovery, the
monitoring API, or boot.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.events import EventLog
from app.recovery import CheckpointStore, ErrorStateStore, RecoveryManager
from app.sessions import SessionStore
from tests.test_research_runner import session  # noqa: F401 — pytest fixture reuse


class TestCorruptedCheckpoints:
    def test_corrupted_payload_degrades_not_crashes(self, db, config, session):
        """A partially-written checkpoint payload must not break reads."""
        store = CheckpointStore(db)
        store.save(session, "heartbeat", {"phase": "discovery"})
        # Simulate corruption at the storage layer.
        with db.tx() as conn:
            conn.execute(
                "UPDATE checkpoints SET payload = '{corrupt' WHERE session_id = ? AND key = 'heartbeat'",
                (session,),
            )
        cp = store.get(session, "heartbeat")
        assert cp is not None, "corrupted row vanished"
        assert cp.payload == {}, "corrupt payload should degrade to empty"
        assert store.get_safe(session, "heartbeat") is not None
        # The list must survive too.
        assert len(store.list(session)) == 1

    def test_non_dict_payload_wrapped(self, db, config, session):
        store = CheckpointStore(db)
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO checkpoints (session_id, key, created_at, payload) VALUES (?, ?, ?, ?)",
                (session, "odd", "2026-01-01", json.dumps("just a string")),
            )
        cp = store.get(session, "odd")
        assert cp is not None
        assert cp.payload == {"_value": "just a string"}

    def test_recovery_manager_survives_corrupted_checkpoints(self, db, config, session):
        """Boot recovery must complete even with corrupt checkpoint rows."""
        store = CheckpointStore(db)
        store.save(session, "phase_complete:x", {"ok": True})
        with db.tx() as conn:
            conn.execute("UPDATE checkpoints SET payload = '{{{' WHERE session_id = ?", (session,))
        sessions = SessionStore(db, config)
        sessions.update(session, status="running")
        errors = ErrorStateStore(db)
        mgr = RecoveryManager(db, sessions, store, errors)
        recovered = mgr.recover_all()
        assert session in recovered
        assert sessions.require(session).status == "interrupted"

    def test_events_table_takes_repeated_identical_appends(self, db, session):
        """Duplicate event appends must not corrupt or error the log."""
        events = EventLog(db)
        e1 = events.append(session, actor="system", action="research_started", detail={"a": 1})
        e2 = events.append(session, actor="system", action="research_started", detail={"a": 1})
        assert e1.id != e2.id, "events are append-only; each append is a row"
        tail = events.tail(session)
        assert len([e for e in tail if e.action == "research_started"]) == 2


from app.events import EventLog  # noqa: E402  (kept below imports-on-top rule)


class TestStorageFailures:
    def test_missing_artifact_file_raises_clear_error(self, db, config, session):
        from app.artifacts import ArtifactError, ArtifactStore

        artifacts = ArtifactStore(db, config)
        art = artifacts.save_text(session, kind="other", filename="probe.txt", text="x")
        # Delete the file behind the artifact row (storage failure).
        __import__("pathlib").Path(art.path).unlink()
        with pytest.raises(ArtifactError, match="missing on disk"):
            artifacts.read_bytes(art)

    def test_artifact_path_escape_rejected(self, db, config, session):
        from app.artifacts import ArtifactError, ArtifactStore

        artifacts = ArtifactStore(db, config)
        evil = artifacts.session_dir(session).parent / "escape.txt"
        evil.write_text("nope")
        with pytest.raises(ArtifactError, match="escapes"):
            artifacts.register_external_file(session, kind="other", path=evil)


class TestRecordingHardening:
    async def test_status_survives_missing_capture_dir(self, db, config):
        """A recording whose capture dir vanished still reports durably."""
        from app.artifacts import ArtifactStore
        from app.live_view import LiveViewStore
        from app.recording import SessionRecorder
        from app.sessions import SessionStore
        from app.trace import ActivityTrace

        sessions = SessionStore(db, config)
        sid = sessions.create(mode="prompt", prompt="x").id
        recorder = SessionRecorder(
            db, config, live_view=LiveViewStore(db, max_frames=2),
            artifacts=ArtifactStore(db, config), trace=ActivityTrace(db),
            browser_manager=None,
        )
        rec = recorder.start(sid)
        # Wipe the capture dir out from under the recording.
        import shutil

        shutil.rmtree(recorder.capture_dir(rec["recording_id"]), ignore_errors=True)
        await asyncio.sleep(0.05)  # let the capture loop observe the loss
        recorder.recover_on_boot()
        row = recorder._row(rec["recording_id"])
        assert row["status"] in ("completed", "failed")
        assert row["artifact_id"] is None  # nothing fabricated
