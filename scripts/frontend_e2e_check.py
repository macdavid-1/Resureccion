"""Frontend e2e: full-stack verification of the Resurrección UI contract.

Exercises exactly what the browser does:
1. Serve index.html + assets.
2. Login with owner credentials.
3. Create a session via /api/sessions.
4. Open the SSE stream and read a snapshot frame.
5. Start a research run and wait for completion.
6. Fetch the final report body.

Run: timeout 110 .venv/bin/python scripts/frontend_e2e_check.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("OWNER_USERNAME", "owner")
os.environ.setdefault("OWNER_PASSWORD", "frontend-e2e-pw")


async def main() -> int:
    tmp = tempfile.mkdtemp(prefix="resurreccion-frontend-e2e-")
    os.environ["DATA_DIR"] = tmp

    from app import config as config_mod
    from app import db as db_mod
    from app.security import hash_password

    os.environ["OWNER_PASSWORD_HASH"] = hash_password("frontend-e2e-pw")
    config_mod.get_config.cache_clear()
    db_mod._db = None

    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app()
    with TestClient(app) as client:
        # 1. Static assets.
        for path, marker in [
            ("/", "Resurrecci"),
            ("/static/styles.css", "--accent"),
            ("/static/app.js", "Resurrecci"),
        ]:
            r = client.get(path)
            assert r.status_code == 200, f"{path}: {r.status_code}"
            assert marker in r.text, f"{path}: missing marker"
        print("[1] static assets serve OK")

        # 2. Login.
        r = client.post("/api/auth/login", json={"username": "owner", "password": "frontend-e2e-pw"})
        assert r.status_code == 200, r.text
        h = {"Cookie": r.headers.get("set-cookie", "").split(";")[0]}
        r = client.get("/api/auth/me", headers=h)
        assert r.status_code == 200
        print("[2] auth OK")

        # 3. Create session.
        r = client.post(
            "/api/sessions",
            json={"prompt": "grief journals for adults", "mode": "prompt"},
            headers=h,
        )
        assert r.status_code in (200, 201), r.text
        sid = r.json()["session"]["id"]
        print(f"[3] session created: {sid[:8]}…")

        # 4. SSE stream frame.
        with client.stream("GET", f"/api/sessions/{sid}/stream?max_events=1", headers=h) as res:
            assert res.status_code == 200
            assert res.headers["content-type"].startswith("text/event-stream")
            buf = ""
            for chunk in res.iter_text():
                buf += chunk
                if "\n\n" in buf:
                    break
            assert "data: " in buf
        print("[4] SSE stream frame OK")

        # 5. Run research to completion (scripted model, direct runner).
        import tests.test_research_runner as trr

        cfg = app.state.config
        app.state.sessions.require(sid)
        runner = trr.make_runner(app.state.db, cfg, trr.ScriptedModel(trr.PHASE_OUTPUTS))
        await runner.run(sid)
        r = client.get(f"/api/sessions/{sid}", headers=h)
        assert r.status_code == 200
        body = r.json()["session"]
        assert body["status"] == "completed", body["status"]
        print(f"[5] research completed (name: {body['name']!r})")

        # 6. Final report (list → latest → body).
        r = client.get(f"/api/sessions/{sid}/reports", headers=h)
        assert r.status_code == 200, r.text
        reps = r.json()["reports"]
        assert reps, "no reports persisted"
        rep = reps[0]
        r = client.get(f"/api/sessions/{sid}/reports/{rep['id']}", headers=h)
        assert r.status_code == 200, r.text
        rep_body = r.json()["body_markdown"]
        # The report body is the model's own document, served verbatim — the
        # backend neither writes it nor stamps a status onto it.
        assert rep_body.strip(), "empty report body"
        assert "**Report status" not in rep_body
        r = client.get(f"/api/sessions/{sid}/reports/{rep['id']}/model", headers=h)
        assert r.status_code == 200, r.text
        stored = r.json()["model"]
        assert stored["ai_report"]["markdown"].strip() == rep_body.strip()
        print(f"[6] final report OK ({len(rep_body)} chars, model-authored)")

        # 7. Chatting about the report (the analyst panel).
        rid = rep["id"]
        r = client.get(f"/api/sessions/{sid}/reports/{rid}/chat", headers=h)
        assert r.status_code == 200 and r.json()["messages"] == []

        class _Chat:
            def __init__(self) -> None:
                self.prompts: list[str] = []

            async def call(self, session_id, *, system, user, **kw):
                self.prompts.append(user)
                reply = type("R", (), {})()
                reply.raw = "The measured opening is the sudden-loss memoir."
                return reply

        chat_model = _Chat()
        app.state.chat_model = chat_model
        r = client.post(
            f"/api/sessions/{sid}/reports/{rid}/chat",
            headers=h, json={"message": "Which niche is strongest?"},
        )
        assert r.status_code == 200, r.text
        assert [m["role"] for m in r.json()["messages"]] == ["owner", "analyst"]
        # The answer's input was the whole record plus the report. The dossier
        # is JSON (newlines escaped), so compare a single-line fragment.
        first_line = next(l for l in rep_body.splitlines() if l.strip())[:60]
        assert first_line in chat_model.prompts[0]
        r = client.post(
            f"/api/sessions/{sid}/reports/{rid}/chat",
            headers=h, json={"message": "And why?"},
        )
        assert r.status_code == 200, r.text
        assert "Which niche is strongest?" in chat_model.prompts[1]
        print("[7] report chat OK (2 turns, full record + thread)")

    print("FRONTEND E2E RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
