"""Durable stores for browser-layer entities.

`browser_auth_states`, `extension_states`, `login_windows`, and
`browser_evidence`. All persist metadata only — never credentials. Cookie and
session material lives exclusively inside the browser profile directory.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from app.db import Database
from app.redact import redact
from app.timeutil import iso_now
from app.timeutil import iso, utcnow
from datetime import timedelta

# Amazon account auth lifecycle (from the owner's perspective).
AUTH_UNKNOWN = "unknown"
AUTH_CONFIRMED = "authenticated"
AUTH_LOGIN_REQUIRED = "login_required"
AUTH_CAPTCHA = "captcha_required"
AUTH_OTP_REQUIRED = "otp_required"
AUTH_SIGNED_OUT = "signed_out"
AUTH_ERROR = "error"
AUTH_STATUSES = (
    AUTH_UNKNOWN, AUTH_CONFIRMED, AUTH_LOGIN_REQUIRED, AUTH_CAPTCHA,
    AUTH_OTP_REQUIRED, AUTH_SIGNED_OUT, AUTH_ERROR,
)

# Extension lifecycle.
EXT_NOT_CONFIGURED = "not_configured"
EXT_INSTALLED = "installed"
EXT_VALIDATED = "validated"
EXT_FAILED = "failed"
EXT_STATUSES = (EXT_NOT_CONFIGURED, EXT_INSTALLED, EXT_VALIDATED, EXT_FAILED)

# Login window lifecycle.
WINDOW_PENDING = "pending"
WINDOW_OPEN = "open"
WINDOW_COMPLETED = "completed"
WINDOW_EXPIRED = "expired"
WINDOW_CANCELLED = "cancelled"
WINDOW_STATUSES = (WINDOW_PENDING, WINDOW_OPEN, WINDOW_COMPLETED, WINDOW_EXPIRED, WINDOW_CANCELLED)


def _new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class AuthState:
    account: str
    profile: str
    status: str
    detail: dict[str, Any]
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        d = redact(self.detail)
        return {
            "account": self.account,
            "profile": self.profile,
            "status": self.status,
            "detail": d,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class ExtensionState:
    name: str
    profile: str
    status: str
    version: str
    extension_id: str
    path: str
    detail: dict[str, Any]
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "profile": self.profile,
            "status": self.status,
            "version": self.version,
            "extension_id": self.extension_id,
            "path": self.path,
            "detail": redact(self.detail),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class LoginWindow:
    id: str
    account: str
    profile: str
    status: str
    created_at: str
    updated_at: str
    expires_at: str
    closed_at: str | None
    result: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "account": self.account,
            "profile": self.profile,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "closed_at": self.closed_at,
            "result": redact(self.result),
        }


@dataclass
class BrowserEvidence:
    id: str
    session_id: str | None
    marketplace: str
    kind: str
    url: str
    title: str
    data: dict[str, Any]
    screenshot_artifact_id: str | None
    captured_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "marketplace": self.marketplace,
            "kind": self.kind,
            "url": self.url,
            "title": self.title,
            "data": redact(self.data),
            "screenshot_artifact_id": self.screenshot_artifact_id,
            "captured_at": self.captured_at,
        }


class BrowserAuthStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def set(
        self,
        account: str,
        profile: str,
        status: str,
        detail: dict[str, Any] | None = None,
    ) -> AuthState:
        if status not in AUTH_STATUSES:
            raise ValueError(f"invalid auth status {status!r}")
        now = iso_now()
        with self.db.tx() as conn:
            conn.execute(
                """
                INSERT INTO browser_auth_states (id, account, profile, status, detail, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account, profile) DO UPDATE SET
                    status = excluded.status,
                    detail = excluded.detail,
                    updated_at = excluded.updated_at
                """,
                (_new_id(), account, profile, status, json.dumps(detail or {}), now, now),
            )
        return self.get(account, profile)  # type: ignore[return-value]

    def get(self, account: str, profile: str) -> AuthState | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM browser_auth_states WHERE account = ? AND profile = ?",
                (account, profile),
            ).fetchone()
        return self._row(row) if row else None

    def list(self, profile: str | None = None) -> list[AuthState]:
        q = "SELECT * FROM browser_auth_states"
        params: list[Any] = []
        if profile is not None:
            q += " WHERE profile = ?"
            params.append(profile)
        q += " ORDER BY updated_at DESC"
        with self.db.read() as conn:
            rows = conn.execute(q, params).fetchall()
        return [self._row(r) for r in rows]

    def _row(self, row: sqlite3.Row) -> AuthState:
        return AuthState(
            account=row["account"],
            profile=row["profile"],
            status=row["status"],
            detail=json.loads(row["detail"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class ExtensionStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(
        self,
        name: str,
        profile: str,
        *,
        status: str,
        version: str = "",
        extension_id: str = "",
        path: str = "",
        detail: dict[str, Any] | None = None,
    ) -> ExtensionState:
        if status not in EXT_STATUSES:
            raise ValueError(f"invalid extension status {status!r}")
        now = iso_now()
        with self.db.tx() as conn:
            conn.execute(
                """
                INSERT INTO extension_states (id, name, profile, status, version, extension_id, path, detail, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name, profile) DO UPDATE SET
                    status = excluded.status,
                    version = excluded.version,
                    extension_id = excluded.extension_id,
                    path = excluded.path,
                    detail = excluded.detail,
                    updated_at = excluded.updated_at
                """,
                (_new_id(), name, profile, status, version, extension_id, path, json.dumps(detail or {}), now, now),
            )
        return self.get(name, profile)  # type: ignore[return-value]

    def get(self, name: str, profile: str) -> ExtensionState | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM extension_states WHERE name = ? AND profile = ?",
                (name, profile),
            ).fetchone()
        return self._row(row) if row else None

    def list(self, profile: str | None = None) -> list[ExtensionState]:
        q = "SELECT * FROM extension_states"
        params: list[Any] = []
        if profile is not None:
            q += " WHERE profile = ?"
            params.append(profile)
        q += " ORDER BY updated_at DESC"
        with self.db.read() as conn:
            rows = conn.execute(q, params).fetchall()
        return [self._row(r) for r in rows]

    def _row(self, row: sqlite3.Row) -> ExtensionState:
        return ExtensionState(
            name=row["name"],
            profile=row["profile"],
            status=row["status"],
            version=row["version"],
            extension_id=row["extension_id"],
            path=row["path"],
            detail=json.loads(row["detail"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class LoginWindowStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, account: str, profile: str, ttl_seconds: int) -> LoginWindow:
        now = utcnow()
        win_id = _new_id()
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO login_windows (id, account, profile, status, created_at, updated_at, expires_at) VALUES (?, ?, ?, 'pending', ?, ?, ?)",
                (win_id, account, profile, iso(now), iso(now), iso(now + timedelta(seconds=ttl_seconds))),
            )
        return self.require(win_id)

    def require(self, window_id: str) -> LoginWindow:
        w = self.get(window_id)
        if w is None:
            raise ValueError(f"login window {window_id} not found")
        return w

    def get(self, window_id: str) -> LoginWindow | None:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM login_windows WHERE id = ?", (window_id,)).fetchone()
        return self._row(row) if row else None

    def list(self, *, status: str | None = None) -> list[LoginWindow]:
        q = "SELECT * FROM login_windows"
        params: list[Any] = []
        if status is not None:
            q += " WHERE status = ?"
            params.append(status)
        q += " ORDER BY created_at DESC"
        with self.db.read() as conn:
            rows = conn.execute(q, params).fetchall()
        return [self._row(r) for r in rows]

    def set_status(self, window_id: str, status: str, result: dict[str, Any] | None = None) -> LoginWindow:
        if status not in WINDOW_STATUSES:
            raise ValueError(f"invalid login window status {status!r}")
        now = iso_now()
        closed = now if status in (WINDOW_COMPLETED, WINDOW_EXPIRED, WINDOW_CANCELLED) else None
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE login_windows SET status = ?, updated_at = ?, closed_at = COALESCE(?, closed_at), result = ? WHERE id = ?",
                (status, now, closed, json.dumps(result or {}), window_id),
            )
        return self.require(window_id)

    def expire_stale(self) -> int:
        now = iso_now()
        with self.db.tx() as conn:
            cur = conn.execute(
                "UPDATE login_windows SET status = 'expired', closed_at = ?, updated_at = ? WHERE status IN ('pending','open') AND expires_at < ?",
                (now, now, now),
            )
            return cur.rowcount

    def _row(self, row: sqlite3.Row) -> LoginWindow:
        return LoginWindow(
            id=row["id"],
            account=row["account"],
            profile=row["profile"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            expires_at=row["expires_at"],
            closed_at=row["closed_at"],
            result=json.loads(row["result"]),
        )


class BrowserEvidenceStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(
        self,
        *,
        session_id: str | None,
        marketplace: str,
        kind: str,
        url: str,
        title: str,
        data: dict[str, Any],
        screenshot_artifact_id: str | None = None,
        captured_at: str | None = None,
    ) -> BrowserEvidence:
        ev_id = _new_id()
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO browser_evidence (id, session_id, marketplace, kind, url, title, data, screenshot_artifact_id, captured_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ev_id, session_id, marketplace, kind, url, title, json.dumps(data), screenshot_artifact_id, captured_at or iso_now()),
            )
        return self.get(ev_id)  # type: ignore[return-value]

    def get(self, evidence_id: str) -> BrowserEvidence | None:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM browser_evidence WHERE id = ?", (evidence_id,)).fetchone()
        return self._row(row) if row else None

    def list(self, session_id: str, *, limit: int = 500) -> list[BrowserEvidence]:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM browser_evidence WHERE session_id = ? ORDER BY captured_at DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [self._row(r) for r in rows]

    def count(self, session_id: str) -> int:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM browser_evidence WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return int(row["n"])

    def _row(self, row: sqlite3.Row) -> BrowserEvidence:
        return BrowserEvidence(
            id=row["id"],
            session_id=row["session_id"],
            marketplace=row["marketplace"],
            kind=row["kind"],
            url=row["url"],
            title=row["title"],
            data=json.loads(row["data"]),
            screenshot_artifact_id=row["screenshot_artifact_id"],
            captured_at=row["captured_at"],
        )
