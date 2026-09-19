"""Live monitoring endpoints: activity trace, timing, SSE stream, live view."""
from __future__ import annotations

import json

import pytest

from app.live_view import LiveViewStore, downscale_jpeg
from app.sessions import SessionStore
from app.timing import TaskType, TimingTracker
from app.trace import ActivityTrace


def _login(client) -> dict:
    res = client.post("/api/auth/login", json={"username": "owner", "password": "hunter2"})
    return {"X-Auth-Token": res.json()["token"]}


def _mk_session(client, headers) -> str:
    res = client.post(
        "/api/sessions", headers=headers,
        json={"mode": "prompt", "prompt": "research grief journals"},
    )
    return res.json()["session"]["id"]


class TestActivityAndTiming:
    def test_activity_requires_auth(self, client):
        assert client.get("/api/sessions/x/activity").status_code in (401, 403)

    def test_activity_unknown_session_404(self, client):
        h = _login(client)
        assert client.get("/api/sessions/nope/activity", headers=h).status_code == 404

    def test_activity_returns_trace_entries(self, client):
        h = _login(client)
        sid = _mk_session(client, h)
        ActivityTrace(client.app.state.db).record(
            sid, "candidate", "Candidate created: grief journals for nurses — rising search variants"
        )
        res = client.get(f"/api/sessions/{sid}/activity", headers=h)
        assert res.status_code == 200
        trace = res.json()["trace"]
        assert trace and "Candidate created" in trace[-1]["text"]
        # Incremental fetch with after_id.
        res2 = client.get(f"/api/sessions/{sid}/activity?after_id={trace[-1]['id']}", headers=h)
        assert res2.json()["trace"] == []

    def test_timing_endpoint(self, client):
        h = _login(client)
        sid = _mk_session(client, h)
        t = TimingTracker(client.app.state.db, sid)
        t.start("k1", TaskType.MODEL_CALL)
        t.finish("k1", outcome="ok")
        res = client.get(f"/api/sessions/{sid}/timing", headers=h)
        assert res.status_code == 200
        body = res.json()
        assert body["by_type"]["model_call"]["count"] == 1


class TestStream:
    def test_stream_requires_auth(self, client):
        assert client.get("/api/sessions/x/stream").status_code in (401, 403)

    def test_stream_emits_snapshots(self, client):
        h = _login(client)
        sid = _mk_session(client, h)
        # Bounded stream (max_events=1) so the test consumes a finite response.
        res = client.get(f"/api/sessions/{sid}/stream?max_events=1", headers=h)
        assert res.status_code == 200
        assert "text/event-stream" in res.headers["content-type"]
        line = next(l for l in res.text.splitlines() if l.startswith("data: "))
        snap = json.loads(line[6:])
        assert snap["session"]["id"] == sid
        assert "counts" in snap and "trace" in snap


class TestLiveView:
    def test_live_frame_empty_204(self, client):
        h = _login(client)
        sid = _mk_session(client, h)
        res = client.get(f"/api/sessions/{sid}/live-view/frame", headers=h)
        assert res.status_code == 204

    def test_live_frame_roundtrip(self, client):
        h = _login(client)
        sid = _mk_session(client, h)
        lv = LiveViewStore(client.app.state.db)
        lv.put(sid, b"jpeg-bytes-here", url="https://amazon.com/s", title="search")
        res = client.get(f"/api/sessions/{sid}/live-view/frame", headers=h)
        assert res.status_code == 200
        assert res.content == b"jpeg-bytes-here"

    def test_ring_is_bounded(self, client):
        h = _login(client)
        sid = _mk_session(client, h)
        lv = LiveViewStore(client.app.state.db, max_frames=3)
        for i in range(10):
            lv.put(sid, f"frame-{i}".encode())
        assert lv.count(sid) == 3
        assert lv.latest(sid).data == b"frame-9"

    def test_oversized_frame_rejected(self, client):
        h = _login(client)
        sid = _mk_session(client, h)
        lv = LiveViewStore(client.app.state.db, max_frame_bytes=10)
        assert lv.put(sid, b"x" * 100) is None

    def test_downscale_never_raises(self):
        assert downscale_jpeg(b"not-an-image") is None
        assert downscale_jpeg(b"") is None
