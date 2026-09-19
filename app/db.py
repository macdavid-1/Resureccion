"""SQLite persistence layer.

Design rules:
- WAL journal mode so readers never block the writer and the DB survives
  abrupt process death.
- `synchronous=FULL` so a commit is durable before the caller proceeds.
- A single shared connection guarded by an RLock. Research runs on one
  process; contention is negligible and correctness matters more.
- `atomic_write` for file artifacts: temp file + fsync + os.replace.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.config import Config, get_config

_SCHEMA_VERSION = 5

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_sessions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    mode TEXT NOT NULL,
    prompt TEXT NOT NULL DEFAULT '',
    objective TEXT NOT NULL DEFAULT '',
    marketplaces TEXT NOT NULL DEFAULT '[]',
    phase TEXT NOT NULL DEFAULT 'initializing',
    progress REAL NOT NULL DEFAULT 0.0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_activity_at TEXT NOT NULL,
    elapsed_seconds REAL NOT NULL DEFAULT 0.0,
    error TEXT,
    resume_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS research_jobs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES research_sessions(id),
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error TEXT,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_jobs_session ON research_jobs(session_id, status);

CREATE TABLE IF NOT EXISTS agent_states (
    session_id TEXT PRIMARY KEY REFERENCES research_sessions(id),
    state TEXT NOT NULL,
    substate TEXT NOT NULL DEFAULT '',
    current_goal TEXT NOT NULL DEFAULT '',
    current_task TEXT NOT NULL DEFAULT '',
    iteration INTEGER NOT NULL DEFAULT 0,
    budget_used_seconds REAL NOT NULL DEFAULT 0.0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observations (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES research_sessions(id),
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_obs_session ON observations(session_id, created_at);

CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES research_sessions(id),
    observation_id TEXT REFERENCES observations(id),
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    uri TEXT NOT NULL,
    summary TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.0,
    meta TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_ev_session ON evidence(session_id, created_at);

CREATE TABLE IF NOT EXISTS candidates (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES research_sessions(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL,
    niche TEXT NOT NULL,
    marketplace TEXT NOT NULL DEFAULT '',
    rationale TEXT NOT NULL DEFAULT '',
    score REAL NOT NULL DEFAULT 0.0,
    meta TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_cand_session ON candidates(session_id, status);

CREATE TABLE IF NOT EXISTS verifications (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES research_sessions(id),
    candidate_id TEXT NOT NULL REFERENCES candidates(id),
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    verdict TEXT NOT NULL DEFAULT '',
    checks TEXT NOT NULL DEFAULT '{}',
    confidence REAL NOT NULL DEFAULT 0.0,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_ver_session ON verifications(session_id, created_at);

CREATE TABLE IF NOT EXISTS opportunities (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES research_sessions(id),
    candidate_id TEXT NOT NULL REFERENCES candidates(id),
    created_at TEXT NOT NULL,
    title TEXT NOT NULL,
    niche TEXT NOT NULL,
    marketplace TEXT NOT NULL DEFAULT '',
    angle TEXT NOT NULL DEFAULT '',
    keywords TEXT NOT NULL DEFAULT '[]',
    projected_demand REAL NOT NULL DEFAULT 0.0,
    projected_competition REAL NOT NULL DEFAULT 0.0,
    confidence REAL NOT NULL DEFAULT 0.0,
    meta TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_opp_session ON opportunities(session_id, created_at);

CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES research_sessions(id),
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    body_path TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_rep_session ON reports(session_id, created_at);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES research_sessions(id),
    created_at TEXT NOT NULL,
    level TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_evt_session ON events(session_id, id);

CREATE TABLE IF NOT EXISTS uploads (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES research_sessions(id),
    created_at TEXT NOT NULL,
    filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    meta TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_upl_session ON uploads(session_id);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES research_sessions(id),
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    meta TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_art_session ON artifacts(session_id, created_at);

CREATE TABLE IF NOT EXISTS export_jobs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES research_sessions(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL,
    format TEXT NOT NULL,
    error TEXT,
    result_path TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_exp_session ON export_jobs(session_id, status);

CREATE TABLE IF NOT EXISTS error_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES research_sessions(id),
    created_at TEXT NOT NULL,
    scope TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    recoverable INTEGER NOT NULL DEFAULT 1,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_err_session ON error_states(session_id, created_at);

CREATE TABLE IF NOT EXISTS checkpoints (
    session_id TEXT NOT NULL REFERENCES research_sessions(id),
    key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (session_id, key)
);

CREATE TABLE IF NOT EXISTS activity_trace (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES research_sessions(id),
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_activity_session ON activity_trace(session_id, id);

CREATE TABLE IF NOT EXISTS task_timings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES research_sessions(id),
    task_key TEXT NOT NULL,
    task_type TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    duration_seconds REAL,
    outcome TEXT NOT NULL DEFAULT 'running',
    attempts INTEGER NOT NULL DEFAULT 1,
    wait_seconds REAL NOT NULL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_timing_session ON task_timings(session_id, id);

CREATE TABLE IF NOT EXISTS live_view_frames (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES research_sessions(id),
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    data BLOB NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_liveview_session ON live_view_frames(session_id, id);

CREATE TABLE IF NOT EXISTS recordings (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES research_sessions(id),
    started_at TEXT NOT NULL,
    stop_requested_at TEXT,
    artifact_id TEXT,
    status TEXT NOT NULL DEFAULT 'recording',
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_recordings_session ON recordings(session_id, id);

CREATE TABLE IF NOT EXISTS browser_sessions (
    session_id TEXT PRIMARY KEY REFERENCES research_sessions(id),
    status TEXT NOT NULL DEFAULT 'none',
    profile TEXT NOT NULL DEFAULT 'default',
    user_agent TEXT NOT NULL DEFAULT '',
    viewport_width INTEGER NOT NULL DEFAULT 1440,
    viewport_height INTEGER NOT NULL DEFAULT 900,
    launch_config TEXT NOT NULL DEFAULT '{}',
    last_health_at TEXT,
    crash_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

-- Owner-controlled browser authentication state (Amazon, KDSpy, etc.).
-- Stores ONLY metadata: status, timestamps, diagnostics. Never credentials,
-- cookies, or tokens — those live exclusively in the browser profile on disk.
CREATE TABLE IF NOT EXISTS browser_auth_states (
    id TEXT PRIMARY KEY,
    account TEXT NOT NULL,
    profile TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (account, profile)
);

-- KDSpy extension installation/validation metadata.
CREATE TABLE IF NOT EXISTS extension_states (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    profile TEXT NOT NULL,
    status TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT '',
    extension_id TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (name, profile)
);

-- One-shot manual login windows: owner opens a live browser window to
-- authenticate Amazon/KDSpy; the window auto-closes and the profile persists.
CREATE TABLE IF NOT EXISTS login_windows (
    id TEXT PRIMARY KEY,
    account TEXT NOT NULL,
    profile TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    closed_at TEXT,
    result TEXT NOT NULL DEFAULT '{}'
);

-- Browser evidence records: structured, redacted research observations.
CREATE TABLE IF NOT EXISTS browser_evidence (
    id TEXT PRIMARY KEY,
    session_id TEXT REFERENCES research_sessions(id),
    marketplace TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL DEFAULT '{}',
    screenshot_artifact_id TEXT,
    captured_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_be_session ON browser_evidence(session_id, captured_at);

-- Claim layer: observation -> evidence -> claim -> interpretation ->
-- verification -> conclusion. Important factual claims that reach a final
-- report must be registered here pointing at the evidence ids they rest on.
CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES research_sessions(id),
    candidate_id TEXT,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    statement TEXT NOT NULL,
    evidence_ids TEXT NOT NULL DEFAULT '[]',
    quality TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL,
    verdict TEXT,
    interpretation TEXT NOT NULL DEFAULT '',
    meta TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_claims_session ON claims(session_id, created_at);

-- Durable integrity assessments: every filter outcome, verification result,
-- quality-gate outcome, KDP risk screening, and report-validation pass is
-- recorded here so the owner can audit WHY any candidate survived or died.
CREATE TABLE IF NOT EXISTS integrity_assessments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES research_sessions(id),
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL DEFAULT '',
    stage TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    metrics TEXT NOT NULL DEFAULT '{}',
    meta TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_ia_session ON integrity_assessments(session_id, kind, created_at);
"""


class Database:
    """Shared SQLite connection with WAL + durability, guarded by a lock."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        with self._lock:
            if self._conn is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # isolation_level=None -> autocommit mode; we manage transactions
            # explicitly in tx() so DDL and DML both participate in rollback.
            conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=10000")
            self._conn = conn
            self._migrate()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Atomic transaction: commits on success, rolls back on error.

        sqlite3 in autocommit mode (isolation_level=None) deactivates a
        transaction on DDL/implicit commit; use in_transaction to detect.
        """
        with self._lock:
            assert self._conn is not None, "database not connected"
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                if self._conn.in_transaction:
                    self._conn.execute("COMMIT")
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            assert self._conn is not None, "database not connected"
            yield self._conn

    # ------------------------------------------------------------- migration
    def _migrate(self) -> None:
        with self.tx() as conn:
            # v2->v3: add export executor columns to existing databases.
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(export_jobs)").fetchall()}
            if cols and "attempts" not in cols:
                conn.execute("ALTER TABLE export_jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
            if cols and "started_at" not in cols:
                conn.execute("ALTER TABLE export_jobs ADD COLUMN started_at TEXT")
            if cols and "finished_at" not in cols:
                conn.execute("ALTER TABLE export_jobs ADD COLUMN finished_at TEXT")
            conn.executescript(_SCHEMA)
            # v4->v5: prune old live-view frames for databases created earlier.
            # (New databases get the table fresh; nothing to migrate.)
            row = conn.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                conn.execute("INSERT INTO schema_version (version) VALUES (?)", (_SCHEMA_VERSION,))
            elif row["version"] < _SCHEMA_VERSION:
                conn.execute("UPDATE schema_version SET version = ?", (_SCHEMA_VERSION,))
            elif row["version"] > _SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema version {row['version']} is newer than supported {_SCHEMA_VERSION}"
                )


_db: Database | None = None


def init_db(config: Config | None = None) -> Database:
    """Create/connect the process-wide database instance."""
    global _db
    cfg = config or get_config()
    cfg.ensure_dirs()
    if _db is None:
        _db = Database(cfg.db_path)
    _db.connect()
    return _db


def get_db() -> Database:
    assert _db is not None, "database not initialized; call init_db() first"
    return _db


def atomic_write_text(path: Path, text: str) -> None:
    """Durably write text: temp file in same dir, fsync, atomic replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
