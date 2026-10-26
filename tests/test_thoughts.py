"""Owner chain of thought: durable rows, the live streaming overlay, the API,
and the runner's summary upgrade.

Raw reasoning is the ONE place it is ever written (owner-only). These tests
pin the three properties that matter: it survives a restart, it updates while
a call streams, and it never leaks out of the session it belongs to.
"""
from __future__ import annotations

import json

import pytest

from app.sessions import SessionStore
from app.thoughts import (
    MAX_DETAIL_CHARS,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_STREAMING,
    ThoughtStore,
)


def _login(client) -> dict:
    res = client.post("/api/auth/login", json={"username": "owner", "password": "hunter2"})
    return {"X-Auth-Token": res.json()["token"]}


def _mk_session(client, headers) -> str:
    res = client.post(
        "/api/sessions", headers=headers,
        json={"mode": "prompt", "prompt": "research grief journals"},
    )
    return res.json()["session"]["id"]


def _session(db, config) -> str:
    return SessionStore(db, config).create(mode="prompt", prompt="research grief journals").id


class TestStore:
    def test_begin_append_finish_is_durable(self, db, config):
        sid = _session(db, config)
        store = ThoughtStore(db)
        tid = store.begin(sid, label="Phase 1 · step 1")
        store.append(tid, "The market ")
        store.append(tid, "looks thin.")
        store.finish(tid, status=STATUS_DONE)

        got = store.get(tid)
        assert got is not None
        assert got.status == STATUS_DONE
        assert got.detail == "The market looks thin."
        assert got.chars == len(got.detail)
        assert got.label == "Phase 1 · step 1"
        # A fresh store over the same database reads the same row — this is
        # persistence, not process memory.
        again = ThoughtStore(db).get(tid)
        assert again is not None and again.detail == "The market looks thin."
        # Finished means no longer live.
        assert store.live(sid) is None

    def test_live_overlay_streams_detail_for_the_open_row(self, db, config):
        sid = _session(db, config)
        store = ThoughtStore(db)
        tid = store.begin(sid, label="Thinking")
        store.append(tid, "weighing demand vs saturation")

        live = store.live(sid)
        assert live is not None
        assert live["id"] == tid
        assert live["status"] == STATUS_STREAMING
        assert live["detail"] == "weighing demand vs saturation"

        # The list payload carries summaries only — the reasoning text is
        # fetched per row on tap, so the SSE snapshot stays small.
        rows = store.recent(sid)
        assert len(rows) == 1
        assert "detail" not in rows[0]
        # …but its length is already honest while the buffer leads the DB.
        assert rows[0]["chars"] == len("weighing demand vs saturation")

    def test_summarize_upgrades_the_summary_line(self, db, config):
        sid = _session(db, config)
        store = ThoughtStore(db)
        tid = store.begin(sid, label="Phase 3 · step 4")
        store.append(tid, "raw reasoning")
        store.finish(tid, status=STATUS_DONE)
        store.summarize(tid, "Opening competitor pages to read review patterns")
        got = store.get(tid)
        assert got.label == "Opening competitor pages to read review patterns"
        assert got.detail == "raw reasoning"  # summary never touches the text

    def test_restart_drops_detail_from_a_discarded_attempt(self, db, config):
        """A stream that had to be replaced must not leave its partial
        reasoning looking like the answer that was actually used."""
        sid = _session(db, config)
        store = ThoughtStore(db)
        tid = store.begin(sid, label="Thinking")
        store.append(tid, "from the attempt we are throwing away")
        store.restart(tid)
        assert store.get(tid).detail == ""
        store.append(tid, "from the real answer")
        store.finish(tid, status=STATUS_DONE)
        assert store.get(tid).detail == "from the real answer"

    def test_recover_stale_stops_claiming_a_dead_call_is_live(self, db, config):
        sid = _session(db, config)
        store = ThoughtStore(db)
        tid = store.begin(sid, label="Thinking")
        store.append(tid, "flushed before the process died")
        store.flush(tid)  # what a process on its way out would ensure
        # Simulate a restart: a new store over the same DB, no live buffer.
        stale = ThoughtStore(db)
        assert stale.live(sid) is not None  # honestly reports the row
        assert stale.recover_stale() == 1
        got = stale.get(tid)
        assert got.status == STATUS_INTERRUPTED
        assert got.detail == "flushed before the process died"  # detail survives
        assert stale.live(sid) is None
        assert stale.recover_stale() == 0  # idempotent

    def test_failed_call_keeps_its_partial_thought(self, db, config):
        sid = _session(db, config)
        store = ThoughtStore(db)
        tid = store.begin(sid, label="Thinking")
        store.append(tid, "started reasoning, then the endpoint 401'd")
        store.finish(tid, status=STATUS_FAILED)
        assert store.get(tid).status == STATUS_FAILED
        assert "401" in store.get(tid).detail

    def test_detail_is_capped(self, db, config):
        sid = _session(db, config)
        store = ThoughtStore(db)
        tid = store.begin(sid, label="Thinking")
        store.append(tid, "x" * (MAX_DETAIL_CHARS + 5_000))
        store.finish(tid, status=STATUS_DONE)
        assert store.get(tid).chars <= MAX_DETAIL_CHARS

    def test_invalid_status_is_a_loud_error(self, db, config):
        sid = _session(db, config)
        store = ThoughtStore(db)
        tid = store.begin(sid, label="Thinking")
        with pytest.raises(ValueError, match="invalid thought status"):
            store.finish(tid, status="whatever")


class TestApi:
    def test_thoughts_require_auth(self, client):
        assert client.get("/api/sessions/x/thoughts").status_code in (401, 403)
        assert client.get("/api/sessions/x/thoughts/1").status_code in (401, 403)

    def test_unknown_session_is_404(self, client):
        h = _login(client)
        assert client.get("/api/sessions/nope/thoughts", headers=h).status_code == 404
        assert client.get("/api/sessions/nope/thoughts/1", headers=h).status_code == 404

    def test_list_returns_summaries_and_detail_fetches_the_text(self, client):
        h = _login(client)
        sid = _mk_session(client, h)
        store = ThoughtStore(client.app.state.db)
        tid = store.begin(sid, label="Phase 1 · step 1")
        store.append(tid, "considering the niche")
        store.finish(tid, status=STATUS_DONE)

        rows = client.get(f"/api/sessions/{sid}/thoughts", headers=h).json()["thoughts"]
        assert [r["label"] for r in rows] == ["Phase 1 · step 1"]
        assert "detail" not in rows[0]

        detail = client.get(f"/api/sessions/{sid}/thoughts/{tid}", headers=h)
        assert detail.status_code == 200
        assert detail.json()["thought"]["detail"] == "considering the niche"

    def test_a_thought_cannot_be_read_through_another_session(self, client):
        h = _login(client)
        sid_a = _mk_session(client, h)
        sid_b = _mk_session(client, h)
        tid = ThoughtStore(client.app.state.db).begin(sid_a, label="private")
        res = client.get(f"/api/sessions/{sid_b}/thoughts/{tid}", headers=h)
        assert res.status_code == 404

    def test_snapshot_carries_summaries_plus_the_streaming_thought(self, client):
        h = _login(client)
        sid = _mk_session(client, h)
        # The app's own store instance — the one the SSE route reads.
        store = client.app.state.thoughts
        done_id = store.begin(sid, label="Finished thought")
        store.finish(done_id, status=STATUS_DONE)
        live_id = store.begin(sid, label="Phase 2 · step 1")
        store.append(live_id, "streaming reasoning in progress")

        res = client.get(f"/api/sessions/{sid}/stream?max_events=1", headers=h)
        assert res.status_code == 200
        line = next(l for l in res.text.splitlines() if l.startswith("data: "))
        snap = json.loads(line[6:])
        assert [t["label"] for t in snap["thoughts"]] == ["Finished thought", "Phase 2 · step 1"]
        assert all("detail" not in t for t in snap["thoughts"])
        assert snap["live_thought"]["id"] == live_id
        assert snap["live_thought"]["detail"] == "streaming reasoning in progress"


class TestRunnerWiring:
    @pytest.mark.asyncio
    async def test_each_model_call_opens_a_labeled_thought_and_summarizes_it(
        self, db, config
    ):
        """The runner labels every thought with where it is in the methodol-
        ogy, then upgrades the summary to the model's own activity_note once
        the reply lands — so a row reads like a headline over the raw text."""
        from tests.test_research_runner import PHASE_OUTPUTS, ScriptedModel, make_runner

        sid = _session(db, config)
        store = ThoughtStore(db)

        class ThoughtfulModel(ScriptedModel):
            def __init__(self, script):
                super().__init__(script)
                self.thoughts = store
                self.labels: list[str | None] = []

            async def call(self, session_id, *, system, user, **k):
                self.labels.append(k.get("label"))
                tid = self.thoughts.begin(session_id, label=k.get("label") or "Thinking")
                self.thoughts.append(tid, "reasoning as it streams")
                reply = await super().call(session_id, system=system, user=user, **k)
                reply.content["activity_note"] = "Opening competitor pages to read reviews"
                self.thoughts.finish(tid, status=STATUS_DONE)
                reply.thought_id = tid
                return reply

        model = ThoughtfulModel(PHASE_OUTPUTS)
        runner = make_runner(db, config, model)
        runner.thoughts = store
        await runner.run(sid)

        assert model.labels, "no model call ever asked for a thought label"
        assert all(isinstance(x, str) and "step" in x for x in model.labels)
        rows = store.recent(sid, limit=200)
        assert rows, "no thoughts were persisted for the run"
        # Every thought the run opened is closed (never left "streaming").
        assert all(r["status"] != STATUS_STREAMING for r in rows)
        # The summary upgrade landed: the runner replaced the phase label.
        assert all(r["label"] == "Opening competitor pages to read reviews" for r in rows)
