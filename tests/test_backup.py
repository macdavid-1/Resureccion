"""Automatic backup tests: snapshot validity, verification, retention, disable.

Backups are the disk-failure half of durability: a snapshot must be a real,
verified SQLite copy, old ones must be pruned so the disk cannot fill, and
disabling must be honored end to end (no files, no loop).
"""
from __future__ import annotations

import sqlite3

import pytest

from app.backup import BackupError, BackupManager


@pytest.fixture()
def mgr(db, config) -> BackupManager:
    return BackupManager(db, config)


def _rows(path) -> int:
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        return conn.execute("SELECT COUNT(*) FROM research_sessions").fetchone()[0]
    finally:
        conn.close()


def test_backup_once_creates_verified_snapshot(mgr, config) -> None:
    out = mgr.backup_once()
    assert out is not None and out.is_file()
    assert out.parent == config.backups_dir
    # The snapshot is a real, openable copy of the live database.
    assert _rows(out) == 0
    # Published snapshot leaves no tmp behind.
    assert not list(config.backups_dir.glob("*.tmp"))


def test_backup_snapshot_copies_existing_data(mgr, db, config) -> None:
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO research_sessions (id, name, status, mode, created_at, updated_at, last_activity_at) "
            "VALUES ('s1', 'n', 'completed', 'auto', 't', 't', 't')"
        )
    out = mgr.backup_once()
    assert out is not None
    assert _rows(out) == 1


def test_verify_rejects_corrupt_snapshot(mgr, tmp_path) -> None:
    bad = tmp_path / "corrupt.db.tmp"
    bad.write_text("{not a database", encoding="utf-8")
    with pytest.raises(BackupError):
        mgr._verify(bad)
    # A failed verification deletes the bad file: never kept as a "backup".
    assert not bad.exists()


def test_prune_keeps_newest_backup_keep(mgr, config) -> None:
    config.backups_dir.mkdir(parents=True, exist_ok=True)
    stem = config.db_path.stem
    names = [f"{stem}-2026010{i}T000000Z-aaaaaa.db" for i in range(5)]
    for n in names:
        (config.backups_dir / n).write_bytes(b"x")
    config.backup_keep = 3
    mgr._prune()
    remaining = sorted(p.name for p in config.backups_dir.glob(f"{stem}-*.db"))
    assert len(remaining) == 3
    # The three newest (by mtime) survive — oldest two are pruned.
    assert names[0] not in remaining and names[1] not in remaining


def test_disabled_backup_writes_nothing(mgr, config, monkeypatch) -> None:
    monkeypatch.setattr(config, "backup_enabled", False)
    assert mgr.backup_once() is None
    assert not list(config.backups_dir.glob("*"))  # no snapshots, no tmp files
    assert mgr.enabled is False


def test_stale_tmp_from_crash_is_cleared(mgr, config) -> None:
    config.backups_dir.mkdir(parents=True, exist_ok=True)
    stale = config.backups_dir / f"{config.db_path.stem}-20200101T000000Z-xxxxxx.db.tmp"
    stale.write_bytes(b"partial")
    out = mgr.backup_once()
    assert out is not None and out.is_file()
    assert not stale.exists()
