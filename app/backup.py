"""Automatic database snapshots — crash-safe is not disk-safe.

app/db.py makes the live database durable against process death (WAL +
synchronous=FULL). This module covers the other failure class: losing the
file itself (disk/volume failure, accidental deletion, corruption). Every
interval the database is snapshotted to a timestamped file under
`DATA_DIR/backups` using SQLite's `VACUUM INTO`, which produces a **consistent
online copy** — safe to run while research is writing, no downtime, no
read-lock on the live database.

Every snapshot is verified with `PRAGMA quick_check` before it is published
(an unverifiable copy is deleted, never kept as a false sense of safety), and
only the newest `BACKUP_KEEP` snapshots are retained so the disk cannot fill
up unattended. Failures are logged and retried at the next interval; they
never crash boot, the supervisor, or research.

Restore is documented in VPS.md. Snapshots protect against corruption and
accidental deletion on the SAME disk; copy `DATA_DIR/backups` off-box (rsync
example in VPS.md) to also cover disk/volume loss.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.config import Config
from app.db import Database

# First snapshot shortly after boot (never during boot itself, so startup IO
# stays focused on recovery), then every BACKUP_INTERVAL_HOURS.
_INITIAL_DELAY_SECONDS = 60


class BackupError(RuntimeError):
    """A snapshot failed and was NOT published."""


class BackupManager:
    """Owns the snapshot lifecycle: one instance per process."""

    def __init__(self, db: Database, config: Config) -> None:
        self.db = db
        self.config = config
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------- one snapshot
    def backup_once(self) -> Path | None:
        """Take, verify, and publish one snapshot. Returns None when disabled.

        Runs synchronously (call via `asyncio.to_thread` from the loop).
        Raises BackupError on any failure after cleaning up partial files.
        """
        cfg = self.config
        if not cfg.backup_enabled:
            return None
        backups = cfg.backups_dir
        backups.mkdir(parents=True, exist_ok=True)

        # Stale .tmp from a snapshot that died mid-write: VACUUM INTO refuses
        # an existing target, so clear it first. Never let a crash block the
        # next backup.
        for stale in backups.glob(f"{cfg.db_path.stem}-*.db.tmp"):
            try:
                stale.unlink()
            except OSError:
                pass

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        # uuid suffix: two snapshots in the same second can never collide.
        final = backups / f"{cfg.db_path.stem}-{stamp}-{uuid.uuid4().hex[:6]}.db"
        tmp = final.with_name(final.name + ".tmp")
        try:
            conn = sqlite3.connect(str(self.db.path), timeout=10, isolation_level=None)
            try:
                conn.execute("PRAGMA busy_timeout=10000")
                # Consistent copy of a live WAL database; the target must not
                # exist (cleared above).
                conn.execute("VACUUM INTO ?", (str(tmp),))
            finally:
                conn.close()
            self._verify(tmp)
            os.replace(tmp, final)  # publish only a verified snapshot
        except BackupError:
            raise
        except sqlite3.Error as exc:
            tmp.unlink(missing_ok=True)
            raise BackupError(f"snapshot failed: {exc}") from exc
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise BackupError(f"snapshot publish failed: {exc}") from exc

        kept = self._prune()
        print(f"[backup] snapshot -> {final.name} ({kept} kept)", flush=True)
        return final

    # ------------------------------------------------------------------ verify
    def _verify(self, path: Path) -> None:
        """quick_check the snapshot; delete it and raise if unreadable."""
        try:
            conn = sqlite3.connect(str(path), isolation_level=None)
            try:
                row = conn.execute("PRAGMA quick_check").fetchone()
            finally:
                conn.close()
            ok = row is not None and row[0] == "ok"
        except sqlite3.Error as exc:
            path.unlink(missing_ok=True)
            raise BackupError(f"snapshot unreadable: {exc}") from exc
        if not ok:
            detail = ", ".join(str(r[0]) for r in [row] if r) or "unknown"
            path.unlink(missing_ok=True)
            raise BackupError(f"snapshot failed quick_check: {detail}")

    # ------------------------------------------------------------------ retention
    def _prune(self) -> int:
        """Keep the newest backup_keep snapshots; delete older ones."""
        backups = self.config.backups_dir
        pattern = f"{self.config.db_path.stem}-*.db"
        # Tie-break by name: the name embeds a UTC timestamp, so on filesystems
        # with coarse mtime granularity (files created in the same second share
        # an mtime) the newest snapshot is still chosen deterministically
        # instead of falling back to arbitrary directory order.
        snaps = sorted(
            (p for p in backups.glob(pattern) if p.is_file()),
            key=lambda p: (p.stat().st_mtime, p.name),
            reverse=True,
        )
        keep = self.config.backup_keep
        for old in snaps[keep:]:
            try:
                old.unlink()
            except OSError:
                pass
        return min(len(snaps), keep)

    # ------------------------------------------------------------------- schedule
    @property
    def enabled(self) -> bool:
        return self.config.backup_enabled

    def start(self) -> None:
        """Arm the periodic snapshot loop (lifespan boot). No-op when disabled."""
        if not self.enabled or self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="backup-loop")

    async def stop(self) -> None:
        """Cancel the loop and wait for it (lifespan shutdown, before db.close)."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        interval = max(1, self.config.backup_interval_hours) * 3600
        # Never let a backup failure kill the loop: log, retry next interval.
        while True:
            await asyncio.sleep(_INITIAL_DELAY_SECONDS)
            try:
                await asyncio.to_thread(self.backup_once)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — a bad backup must not be fatal
                print(f"[backup] snapshot failed: {exc}", flush=True)
            await asyncio.sleep(interval - _INITIAL_DELAY_SECONDS)
