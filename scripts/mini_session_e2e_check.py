"""Mini-session e2e: the whole loop over the real HTTP API.

Exercises exactly what the browser does:
1. Serve the page and assets (the mini-session UI is shipped).
2. Login and create a session.
3. Run the research to completion (scripted model).
4. Ask the analyst a question; the answer proposes a mini session.
5. Start that mini session, type guidance into the conversation while it works,
   and wait for its answer to land back in the same thread.
6. Run a second mini session in the same conversation (no limit).
7. Cancel a third one and see it stop.
8. Confirm a follow-up run's conversation gives a mini session the record of
   both runs.

The model and the browser are scripted/stubbed here (no API key and no browser
in this workspace), but everything else — routes, auth, store, dossier, chat,
step log, worker task — is the production path.

Run: timeout 170 .venv/bin/python scripts/mini_session_e2e_check.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("OWNER_USERNAME", "owner")
os.environ.setdefault("OWNER_PASSWORD", "mini-e2e-pw")


def _wait(client, headers, sid, mid, *, timeout=30.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        r = client.get(f"/api/sessions/{sid}/mini-sessions/{mid}", headers=headers)
        assert r.status_code == 200, r.text
        last = r.json()
        if last["mini_session"]["status"] not in ("queued", "running"):
            return last
        time.sleep(0.02)
    raise AssertionError(f"mini session never finished: {last}")


class _MiniModel:
    """A scripted mini-session model: two actions, then a cited answer."""

    def __init__(self, *, cite: str, guide_probe: str | None = None) -> None:
        self.cite = cite
        self.guide_probe = guide_probe
        self.prompts: list[str] = []

    async def call(self, session_id, *, system, user, **kw):
        # A real model call takes seconds; the pause lets the owner's message
        # arrive while the session is genuinely working.
        await asyncio.sleep(0.15)
        self.prompts.append(user)
        assert "THE RESEARCH RECORD (complete" in user, "the record must be complete"
        assert "NOT bound" in user, "the methodology must be reference, not a cage"
        nxt: dict
        if len(self.prompts) == 1:
            nxt = {"note": "opening the category",
                   "actions": [{"action": "open_category", "args": {"path": "books"}}]}
        elif len(self.prompts) == 2:
            if self.guide_probe:
                assert self.guide_probe in user, "the owner's mid-run message must arrive"
            nxt = {"note": "reading the top book",
                   "actions": [{"action": "open_product", "args": {"asin": "B00MINI"}}]}
        else:
            nxt = {"note": "settled", "actions": [], "done": True,
                   "answer": f"The price is unchanged at $8.99 [ev:{self.cite}]."}
        reply = type("R", (), {})()
        reply.content = nxt
        reply.raw = json.dumps(nxt)
        return reply


async def main() -> int:
    tmp = tempfile.mkdtemp(prefix="resurreccion-mini-e2e-")
    os.environ["DATA_DIR"] = tmp

    from app import config as config_mod
    from app import db as db_mod
    from app.security import hash_password

    os.environ["OWNER_PASSWORD_HASH"] = hash_password("mini-e2e-pw")
    config_mod.get_config.cache_clear()
    db_mod._db = None

    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app()
    with TestClient(app) as client:
        # 1. The UI ships the mini-session surface.
        page = client.get("/static/app.js")
        assert page.status_code == 200
        for marker in ("startMiniSession", "chat-proposal", "chat-mini"):
            assert marker in page.text, f"app.js is missing {marker}"
        css = client.get("/static/styles.css")
        assert ".chat-proposal" in css.text and ".chat-mini" in css.text
        print("[1] mini-session UI assets serve OK")

        # 2. Login + session.
        r = client.post("/api/auth/login",
                        json={"username": "owner", "password": "mini-e2e-pw"})
        assert r.status_code == 200, r.text
        h = {"Cookie": r.headers.get("set-cookie", "").split(";")[0]}
        r = client.post("/api/sessions", headers=h,
                        json={"prompt": "grief memoirs for adult children", "mode": "prompt"})
        assert r.status_code in (200, 201), r.text
        sid = r.json()["session"]["id"]
        print("[2] auth + session OK")

        # 3. Real research run (scripted model, real runner).
        import tests.test_research_runner as trr

        runner = trr.make_runner(app.state.db, app.state.config,
                                 trr.ScriptedModel(trr.PHASE_OUTPUTS))
        await runner.run(sid)
        reps = client.get(f"/api/sessions/{sid}/reports", headers=h).json()["reports"]
        final = [x for x in reps if x["kind"] == "final"]
        assert final, "the run produced no final report"
        rid = final[0]["id"]
        evidence = app.state.browser_evidence.list(sid, limit=10_000_000)
        assert evidence, "the run captured no evidence"
        cite = evidence[0].id
        print(f"[3] research completed ({len(evidence)} captures)")

        # 4. The answer proposes a mini session; nothing starts by itself.
        class _Proposer:
            async def call(self, session_id, *, system, user, **kw):
                reply = type("R", (), {})()
                goal = "Verify the #1 grief memoir still sells at $8.99"
                why = "the ceiling may have moved"
                reply.raw = (
                    "The record prices it at $8.99.\n\n```mini-session\n"
                    + json.dumps({"goal": goal, "why": why})
                    + "\n```"
                )
                return reply

        app.state.chat_model = _Proposer()
        r = client.post(f"/api/sessions/{sid}/reports/{rid}/chat", headers=h,
                        json={"message": "is that price still right?"})
        assert r.status_code == 200, r.text
        answer = r.json()["answer"]
        proposal = answer["meta"].get("proposal")
        assert proposal and proposal["goal"].startswith("Verify the #1 grief memoir"), answer
        assert "mini-session" not in answer["text"], "the block leaked into the bubble"
        assert client.get(f"/api/sessions/{sid}/reports/{rid}/mini-sessions",
                          headers=h).json()["mini_sessions"] == []
        print(f"[4] mini session proposed (not started): {proposal['goal'][:48]}…")

        # 5. Start it, guide it mid-run, get its answer in the thread.
        class _Executor:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict]] = []

            async def execute(self, *, session_id, action, args):
                self.calls.append((action, dict(args or {})))
                return type("O", (), {"ok": True, "summary": f"{action} ok",
                                      "error": "", "evidence_ids": []})()

        executor = _Executor()
        app.state.mini_executor = executor
        guide = "also check the UK price"
        model = _MiniModel(cite=cite, guide_probe=guide)
        app.state.mini_model = model

        service = app.state.mini_sessions
        original = model.call

        async def _call(session_id, *, system, user, **kw):
            # Simulate the owner typing into the conversation while it works.
            if not model.prompts:
                service.deliver_owner_message(rid, guide)
            return await original(session_id, system=system, user=user, **kw)

        model.call = _call
        r = client.post(f"/api/sessions/{sid}/reports/{rid}/mini-sessions", headers=h,
                        json={"goal": proposal["goal"], "title": "price check",
                              "proposal_message_id": answer["id"]})
        assert r.status_code == 200, r.text
        mini_id = r.json()["mini_session"]["id"]
        # A message typed now goes to the running session, not to the analyst.
        r = client.post(f"/api/sessions/{sid}/reports/{rid}/chat", headers=h,
                        json={"message": "focus on the paperback"})
        assert r.status_code == 200, r.text
        assert r.json().get("delivered_to_mini_session") == mini_id, r.text
        done = _wait(client, headers=h, sid=sid, mid=mini_id)
        mini = done["mini_session"]
        assert mini["status"] == "completed", mini["error"]
        assert f"/api/sessions/{sid}/evidence/{cite}" in mini["result"]
        assert mini["meta"]["proposal_message_id"] == answer["id"]
        assert [c[0] for c in executor.calls] == ["open_category", "open_product"], executor.calls
        assert any(s["kind"] == "owner" and "paperback" in s["text"] for s in done["steps"])
        assert [s["meta"].get("summary") for s in done["steps"] if s["kind"] == "action"] == [
            "open_category ok", "open_product ok",
        ]
        thread = client.get(f"/api/sessions/{sid}/reports/{rid}/chat", headers=h).json()["messages"]
        result_msg = [m for m in thread if (m.get("meta") or {}).get("mini_session_id") == mini_id]
        assert result_msg, "the mini session's answer is not in the conversation"
        assert "$8.99" in result_msg[0]["text"]
        assert result_msg[0]["role"] == "analyst"
        print(f"[5] mini session ran ({len(done['steps'])} steps) and answered in the thread")

        # 6. A second mini session in the same conversation — no limit.
        app.state.mini_model = _MiniModel(cite=cite)
        r = client.post(f"/api/sessions/{sid}/reports/{rid}/mini-sessions", headers=h,
                        json={"goal": "Count the reviews on the #1 book"})
        second = r.json()["mini_session"]["id"]
        second_done = _wait(client, headers=h, sid=sid, mid=second)
        assert second_done["mini_session"]["status"] == "completed"
        listed = client.get(f"/api/sessions/{sid}/reports/{rid}/mini-sessions",
                            headers=h).json()["mini_sessions"]
        assert {m["id"] for m in listed} >= {mini_id, second}
        print(f"[6] second mini session ran in the same conversation ({len(listed)} total)")

        # 7. A running mini session can be cancelled by the owner.
        cancel_holder = {"id": ""}

        class _Slow:
            async def call(self, session_id, *, system, user, **kw):
                while not service._cancel.get(cancel_holder["id"]):
                    await asyncio.sleep(0.01)
                return type("R", (), {"content": {"note": "stopped", "actions": [],
                                                  "done": False, "answer": ""}})()

        app.state.mini_model = _Slow()
        r = client.post(f"/api/sessions/{sid}/reports/{rid}/mini-sessions", headers=h,
                        json={"goal": "something long"})
        cancel_holder["id"] = r.json()["mini_session"]["id"]
        await asyncio.sleep(0.1)  # let the loop start before cancelling it
        r = client.post(f"/api/sessions/{sid}/mini-sessions/{cancel_holder['id']}/cancel",
                        headers=h)
        assert r.status_code == 200, r.text
        cancelled = _wait(client, headers=h, sid=sid, mid=cancel_holder["id"])
        assert cancelled["mini_session"]["status"] == "cancelled", cancelled["mini_session"]
        print("[7] owner cancel stops a running mini session")

        # 8. A follow-up run's mini session sees BOTH runs.
        app.state.mini_model = None
        r = client.post(f"/api/sessions/{sid}/follow-up", headers=h,
                        json={"prompt": "now check the UK market"})
        assert r.status_code in (200, 201), r.text
        follow_sid = r.json()["session"]["id"]
        await trr.make_runner(app.state.db, app.state.config,
                              trr.ScriptedModel(trr.PHASE_OUTPUTS)).run(follow_sid)
        follow_reps = client.get(f"/api/sessions/{follow_sid}/reports", headers=h).json()["reports"]
        follow_final = next(x for x in follow_reps if x["kind"] == "final")
        follow_model = _MiniModel(cite=cite)
        app.state.mini_model = follow_model
        r = client.post(
            f"/api/sessions/{follow_sid}/reports/{follow_final['id']}/mini-sessions",
            headers=h, json={"goal": "check the UK #1 price"},
        )
        follow_mini = r.json()["mini_session"]["id"]
        follow_done = _wait(client, headers=h, sid=follow_sid, mid=follow_mini)
        assert follow_done["mini_session"]["status"] == "completed", follow_done["mini_session"]
        prompt = follow_model.prompts[0]
        assert "prior_runs" in prompt, "the follow-up's earlier run is missing"
        assert "grief memoirs for adult children" in prompt, "the first run's brief is missing"
        print("[8] follow-up mini session carries the earlier runs")

    print("MINI SESSION E2E RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
