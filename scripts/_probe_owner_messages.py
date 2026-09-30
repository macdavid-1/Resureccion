"""E2E probe: owner→agent message channel end to end.

1. Simulates an EXISTING v5 database and verifies the v5->v6 migration
   creates the owner_messages table.
2. Boots the full app and exercises the API flow: auth, POST /messages,
   GET /messages, and the SSE snapshot carrying owner_messages.

Run with:  HOME=/root DATA_DIR=$(mktemp -d) .venv/bin/python scripts/_probe_owner_messages.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# 1. Simulate an EXISTING v5 database, then boot the app over it.
tmp = pathlib.Path(os.environ["DATA_DIR"])
tmp.mkdir(parents=True, exist_ok=True)
db_path = tmp / "old.db"
conn = sqlite3.connect(db_path)
conn.executescript(
    """
CREATE TABLE schema_version (version INTEGER NOT NULL);
INSERT INTO schema_version VALUES (5);
CREATE TABLE research_sessions (id TEXT PRIMARY KEY, name TEXT, status TEXT, mode TEXT, prompt TEXT DEFAULT '', objective TEXT DEFAULT '', marketplaces TEXT DEFAULT '[]', phase TEXT DEFAULT 'initializing', progress REAL DEFAULT 0.0, created_at TEXT, updated_at TEXT, last_activity_at TEXT, elapsed_seconds REAL DEFAULT 0.0, error TEXT, resume_count INTEGER DEFAULT 0);
"""
)
conn.commit()
conn.close()

from app.config import get_config

cfg = get_config()
cfg.db_path = db_path
from app.db import init_db

db = init_db(cfg)
with db.read() as c:
    v = c.execute("SELECT version FROM schema_version").fetchone()["version"]
    cols = {r["name"] for r in c.execute("PRAGMA table_info(owner_messages)").fetchall()}
assert v == 6 and {"id", "session_id", "created_at", "text", "delivered_at"} <= cols, (v, cols)
print("migration v5->v6 OK, owner_messages columns:", sorted(cols))

# 2. Boot the full app and exercise the endpoint flow.
from fastapi.testclient import TestClient

from app.main import create_app

app = create_app()
print("app OK (routers included)")

from app.security import hash_password

cfg.owner_password_hash = hash_password("hunter2")
with TestClient(app) as c:
    tok = c.post("/api/auth/login", json={"username": "owner", "password": "hunter2"}).json()["token"]
    h = {"X-Auth-Token": tok}
    sid = c.post("/api/sessions", headers=h, json={"mode": "auto"}).json()["session"]["id"]
    r = c.post(f"/api/sessions/{sid}/messages", headers=h, json={"text": "prioritize UK market"})
    assert r.status_code == 200, r.text
    r2 = c.get(f"/api/sessions/{sid}/messages", headers=h)
    assert r2.json()["messages"][0]["text"] == "prioritize UK market"
    # SSE snapshot carries owner_messages for the UI.
    with c.stream("GET", f"/api/sessions/{sid}/stream", headers=h, params={"max_events": 1}) as s:
        for chunk in s.iter_text():
            if chunk.startswith("data: "):
                snap = json.loads(chunk[6:])
                assert snap["owner_messages"][0]["text"] == "prioritize UK market"
                print(
                    "SSE snapshot owner_messages OK, delivered =",
                    snap["owner_messages"][0]["delivered"],
                )
                break
print("ALL BOOT CHECKS PASSED")
