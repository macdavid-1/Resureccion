"""Frontend-stage APIs: session naming, recording lifecycle, report file
download, and the recording artifact route."""
from __future__ import annotations

import asyncio

import pytest

from app.naming import _valid_name, fallback_name


def _login(client) -> dict:
    res = client.post("/api/auth/login", json={"username": "owner", "password": "hunter2"})
    return {"X-Auth-Token": res.json()["token"]}


def _mk_session(client, headers, mode="prompt", prompt="research grief journals") -> str:
    res = client.post("/api/sessions", headers=headers, json={"mode": mode, "prompt": prompt})
    return res.json()["session"]["id"]


class TestNaming:
    def test_valid_names_pass(self):
        assert _valid_name("Sudden-loss grief journals — US sweep")
        assert _valid_name("Estate planners for widowed fathers")

    def test_generic_names_rejected(self):
        assert _valid_name("Research Session 14") is None
        assert _valid_name("untitled") is None
        assert _valid_name("new session") is None

    def test_length_bounds(self):
        assert _valid_name("ab") is None
        assert _valid_name("x" * 200) is None

    def test_fallback_uses_objective_words(self):
        assert "prayer" in fallback_name("prompt", "research prayer journals", "")

    def test_fallback_auto_mode(self):
        assert fallback_name("auto", "", "") == "Autonomous Market Sweep"


class TestRecordingAPI:
    def test_recording_requires_auth(self, client):
        assert client.post("/api/sessions/x/recording/start").status_code in (401, 403)

    def test_recording_unknown_session_404(self, client):
        h = _login(client)
        assert client.post("/api/sessions/nope/recording/start", headers=h).status_code == 404

    def test_recording_lifecycle(self, client):
        h = _login(client)
        sid = _mk_session(client, h)
        res = client.post(f"/api/sessions/{sid}/recording/start", headers=h)
        assert res.status_code == 200
        rec = res.json()["recording"]
        assert rec["active"] is True
        # Status endpoint reflects it.
        res = client.get(f"/api/sessions/{sid}/recording", headers=h)
        assert res.json()["recording"]["session_id"] == sid
        # Stop: no live browser in tests, so frames may be empty; the stop
        # itself must succeed and produce a final state.
        res = client.post(f"/api/sessions/{sid}/recording/stop", headers=h)
        assert res.status_code == 200
        body = res.json()["recording"]
        assert body["stop_requested"] is True

    def test_stop_without_start_404(self, client):
        h = _login(client)
        sid = _mk_session(client, h)
        assert client.post(f"/api/sessions/{sid}/recording/stop", headers=h).status_code == 404


class TestRecordingDurability:
    """Recording is server-side and must survive a restart: frames stream to
    disk, state is durable, and boot recovery finalizes orphans."""

    def _recorder(self, db, config, trace):
        from app.artifacts import ArtifactStore
        from app.live_view import LiveViewStore
        from app.recording import SessionRecorder

        return SessionRecorder(
            db, config,
            live_view=LiveViewStore(db, max_frames=4),
            artifacts=ArtifactStore(db, config),
            trace=trace,
            browser_manager=None,
        )

    def _new_session(self, db, config) -> str:
        """Recordings FK-reference sessions; create a real row."""
        from app.sessions import SessionStore

        return SessionStore(db, config).create(mode="prompt", prompt="durability test").id

    # 1x1 red PNG — no PIL needed in the test env.
    _PNG = bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001080200000090"
        "7753de0000000c4944415408d763f8cfc00000030101"
        "00c9fe92ef0000000049454e44ae426082"
    )

    def _frame_png(self, color=(200, 30, 30)) -> bytes:
        return self._PNG

    async def test_frames_stream_to_disk_and_recover(self, db, config, monkeypatch):
        """A restart mid-recording: durable row + on-disk frames finalize into
        a real artifact instead of vanishing."""
        from app.trace import ActivityTrace

        sid = self._new_session(db, config)
        trace = ActivityTrace(db)
        recorder = self._recorder(db, config, trace)
        rec = recorder.start(sid)
        rid = rec["recording_id"]
        cap_dir = recorder.capture_dir(rid)
        # Simulate the capture loop's disk writes (no browser in tests).
        (cap_dir / "meta.json").write_text(
            __import__("json").dumps({"started_epoch": 1_000_000.0})
        )
        png = self._frame_png()
        for i in range(3):
            recorder._write_frame(cap_dir, i, png)
        with (cap_dir / "trace.jsonl").open("w", encoding="utf-8") as fh:
            fh.write(__import__("json").dumps(
                {"t": 1_000_001.0, "kind": "candidate", "text": "Candidate created: x", "at": "2026-01-01T00:00:00Z"}
            ) + "\n")
        # Durable row exists.
        row = recorder._row(rid)
        assert row is not None and row["status"] == "recording"

        # The process dies; a NEW recorder instance boots and recovers.
        recorder2 = self._recorder(db, config, ActivityTrace(db))
        recovered = recorder2.recover_on_boot()
        assert rid in recovered
        row = recorder2._row(rid)
        assert row["status"] == "completed"
        assert row["artifact_id"]
        art = __import__("app.artifacts", fromlist=["ArtifactStore"]).ArtifactStore(db, config).get(sid, row["artifact_id"])
        assert art is not None
        # WebM when imageio-ffmpeg is available, HTML bundle otherwise —
        # either way exactly one real, downloadable artifact.
        assert art.meta["format"] in ("webm", "html_bundle")
        assert art.meta["frames"] == 3
        # Capture dir cleaned up after finalize.
        assert not cap_dir.exists()

    async def test_recover_skips_empty_captures(self, db, config):
        from app.trace import ActivityTrace

        sid = self._new_session(db, config)
        recorder = self._recorder(db, config, ActivityTrace(db))
        rec = recorder.start(sid)
        recorder.recover_on_boot()
        row = recorder._row(rec["recording_id"])
        # Nothing on disk -> completed with no artifact, not a fake video.
        assert row["status"] == "completed"
        assert row["artifact_id"] is None

    async def test_live_ring_fallback_frame(self, db, config, monkeypatch):
        """No live browser: the bounded live-view ring still feeds frames."""
        from app.live_view import LiveViewStore
        from app.trace import ActivityTrace

        sid = self._new_session(db, config)
        lv = LiveViewStore(db, max_frames=4)
        lv.put(sid, self._frame_png((10, 120, 250)), kind="screenshot")
        recorder = self._recorder(db, config, ActivityTrace(db))
        recorder.live_view = lv
        rec = recorder.start(sid)
        await        asyncio.sleep(0.8)  # let the loop capture at least one tick
        await recorder.stop(sid)
        row = recorder._row(rec["recording_id"])
        assert row["status"] == "completed"
        assert row["artifact_id"] is not None


class TestWebmEncoding:
    """The WebM video path — real frames, real ffmpeg encode."""

    def _recorder(self, db, config, trace, browser=None):
        from app.artifacts import ArtifactStore
        from app.live_view import LiveViewStore
        from app.recording import SessionRecorder

        return SessionRecorder(
            db, config,
            live_view=LiveViewStore(db, max_frames=8),
            artifacts=ArtifactStore(db, config),
            trace=trace,
            browser_manager=browser,
        )

    def _png(self, size=(640, 400), color=(20, 60, 220)) -> bytes:
        import io

        from PIL import Image

        img = Image.new("RGB", size, color)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    async def test_webm_produced_from_browser_frames(self, db, config):
        """With imageio-ffmpeg installed, a recording of real browser PNG
        frames finalizes to a valid, playable WebM video artifact."""
        pytest.importorskip("imageio")
        pytest.importorskip("imageio_ffmpeg")

        from app.sessions import SessionStore
        from app.trace import ActivityTrace

        sid = SessionStore(db, config).create(mode="prompt", prompt="webm test").id

        class FakeBrowser:
            async def live_screenshot(self, session_id: str) -> bytes:
                return self._png((640, 400), (20, 60, 220))

        fb = FakeBrowser()
        fb._png = self._png
        recorder = self._recorder(db, config, ActivityTrace(db), browser=fb)
        rec = recorder.start(sid)
        await asyncio.sleep(1.3)  # ~2 frames at 2fps
        trace = ActivityTrace(db)
        trace.record(sid, "candidate", "Candidate created: webm probe niche")
        await asyncio.sleep(0.4)
        await recorder.stop(sid)

        row = recorder._row(rec["recording_id"])
        assert row["status"] == "completed"
        assert row["artifact_id"]
        from app.artifacts import ArtifactStore

        art = ArtifactStore(db, config).get(sid, row["artifact_id"])
        assert art is not None
        assert art.meta["frames"] >= 1
        # The video carries the burned-in research HUD.
        assert art.meta.get("hud") is True
        assert art.path.endswith(".webm")
        data = ArtifactStore(db, config).read_bytes(art)
        # EBML magic — a real Matroska/WebM container, not an error page.
        assert data[:4] == b"\x1aE\xdf\xa3"
        assert len(data) > 1000
        # Capture directory fully cleaned up.
        assert not recorder.capture_dir(rec["recording_id"]).exists()

    async def test_webm_pipeline_survives_odd_frames(self, db, config):
        """Corrupt frames interleaved with good ones: encoder skips junk,
        recording still completes with a playable artifact."""
        pytest.importorskip("imageio")
        pytest.importorskip("imageio_ffmpeg")

        from app.sessions import SessionStore
        from app.trace import ActivityTrace

        sid = SessionStore(db, config).create(mode="prompt", prompt="corrupt test").id

        class FlickerBrowser:
            def __init__(self):
                self.n = 0

            async def live_screenshot(self, session_id: str) -> bytes:
                self.n += 1
                if self.n % 2 == 0:
                    return b"\x89PNG\r\n\x1a\nGARBAGE-NOT-A-REAL-IMAGE"
                return TestWebmEncoding._png_static()

        recorder = self._recorder(db, config, ActivityTrace(db), browser=FlickerBrowser())
        rec = recorder.start(sid)
        await asyncio.sleep(1.3)
        await recorder.stop(sid)
        row = recorder._row(rec["recording_id"])
        assert row["status"] == "completed"
        assert row["artifact_id"]

    @staticmethod
    def _png_static() -> bytes:
        import io

        from PIL import Image

        img = Image.new("RGB", (640, 400), (90, 20, 200))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()


class TestHudBurn:
    """The burned-in recording HUD (timecode + current activity)."""

    def test_hud_darkens_bottom_bar(self):
        from PIL import Image

        from app.recording import _burn_hud

        img = Image.new("RGB", (640, 400), (250, 250, 250))  # bright page
        px_before = img.getpixel((320, 395))
        _burn_hud(img, 75.0, {"kind": "candidate", "text": "Candidate created: grief journals"})
        px_after = img.getpixel((320, 395))
        # Bottom bar went bright -> dark.
        assert sum(px_before) > 700
        assert sum(px_after) < 250

    def test_hud_timecode_formatting(self):
        from app.recording import _fmt_tc

        assert _fmt_tc(0) == "00:00"
        assert _fmt_tc(75) == "01:15"
        assert _fmt_tc(3672) == "1:01:12"


class TestReportAndRecordingFiles:
    def test_report_download(self, client):
        h = _login(client)
        sid = _mk_session(client, h)
        res = client.post(
            f"/api/sessions/{sid}/reports", headers=h,
            json={"kind": "intermediate", "title": "T", "body_markdown": "# Body"},
        )
        rid = res.json()["report"]["id"]
        res = client.get(f"/api/sessions/{sid}/reports/{rid}/file", headers=h)
        assert res.status_code == 200
        assert "markdown" in res.headers["content-type"]
        assert "# Body" in res.text

    def test_report_download_404(self, client):
        h = _login(client)
        sid = _mk_session(client, h)
        assert client.get(f"/api/sessions/{sid}/reports/nope/file", headers=h).status_code == 404

    def test_recording_download_wrong_kind_404(self, client):
        h = _login(client)
        sid = _mk_session(client, h)
        res = client.post(
            f"/api/sessions/{sid}/reports", headers=h,
            json={"kind": "intermediate", "title": "T", "body_markdown": "# B"},
        )
        rid = res.json()["report"]["id"]
        assert client.get(
            f"/api/sessions/{sid}/recordings/{rid}/file", headers=h
        ).status_code == 404


class TestFrontendServing:
    def test_index_served(self, client):
        res = client.get("/")
        assert res.status_code == 200
        assert "Resurrección" in res.text
        assert "tabbar" in res.text

    def test_static_assets_served(self, client):
        assert client.get("/static/styles.css").status_code == 200
        res = client.get("/static/app.js")
        assert res.status_code == 200
        assert "EventSource" in res.text
