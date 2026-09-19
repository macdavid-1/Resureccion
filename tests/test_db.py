"""Tests for the SQLite persistence layer."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.db import atomic_write_bytes, atomic_write_text


def test_connect_creates_schema(db) -> None:
    with db.read() as conn:
        tables = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    expected = {
        "schema_version", "users", "auth_sessions", "research_sessions", "research_jobs",
        "agent_states", "observations", "evidence", "candidates", "verifications",
        "opportunities", "reports", "events", "uploads", "artifacts", "export_jobs",
        "error_states", "checkpoints", "browser_sessions",
    }
    assert expected <= tables


def test_journal_mode_is_wal(db) -> None:
    with db.read() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_transaction_rolls_back_on_error(db) -> None:
    with pytest.raises(RuntimeError):
        with db.tx() as conn:
            conn.execute("CREATE TABLE rollback_test (x INTEGER)")
            raise RuntimeError("boom")
    with db.read() as conn:
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "rollback_test" not in tables


def test_atomic_write_text(tmp_path: Path) -> None:
    p = tmp_path / "sub" / "file.txt"
    atomic_write_text(p, "hello")
    assert p.read_text() == "hello"
    assert not p.with_name(p.name + ".tmp").exists()


def test_atomic_write_bytes(tmp_path: Path) -> None:
    p = tmp_path / "sub" / "blob.bin"
    atomic_write_bytes(p, b"\x00\x01")
    assert p.read_bytes() == b"\x00\x01"
