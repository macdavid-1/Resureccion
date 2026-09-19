"""Single-user authentication.

Exactly one user exists (row id = 1 in `users`). The owner logs in with
username + password; successful login creates an auth session whose token is
stored hashed (SHA-256) so a DB leak does not leak usable tokens. The token is
set as an HttpOnly cookie and must also be sent as `X-Auth-Token` by API clients.

Password hashing uses hashlib.scrypt with a per-user random salt.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from datetime import timedelta

from app.config import Config
from app.db import Database
from app.timeutil import iso, iso_now, parse_iso, utcnow

_TOKEN_BYTES = 32


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, digest_hex = stored.split("$")
        if algo != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401) -> None:
        super().__init__(message)
        self.status = status


class AuthService:
    """Single-owner auth backed by SQLite."""

    def __init__(self, db: Database, config: Config) -> None:
        self.db = db
        self.config = config
        self._lock = threading.Lock()
        self._failed_attempts: list[float] = []
        self._max_attempts = 10
        self._window_seconds = 300

    # ------------------------------------------------------------------ setup
    def ensure_user(self) -> None:
        """Create the single owner row if missing (idempotent)."""
        cfg = self.config
        if not cfg.owner_password_hash:
            raise RuntimeError(
                "OWNER_PASSWORD_HASH is not set; generate with python -m app.scripts.set_password"
            )
        with self.db.tx() as conn:
            row = conn.execute("SELECT id FROM users WHERE id = 1").fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO users (id, username, password_hash, created_at) VALUES (1, ?, ?, ?)",
                    (cfg.owner_username, cfg.owner_password_hash, iso_now()),
                )
            else:
                conn.execute(
                    "UPDATE users SET username = ?, password_hash = ? WHERE id = 1",
                    (cfg.owner_username, cfg.owner_password_hash),
                )

    # ------------------------------------------------------------------ login
    def login(self, username: str, password: str) -> str:
        """Verify credentials, return a fresh bearer token. Raises AuthError."""
        now = time.monotonic()
        with self._lock:
            self._failed_attempts = [t for t in self._failed_attempts if now - t < self._window_seconds]
            if len(self._failed_attempts) >= self._max_attempts:
                raise AuthError("Too many failed attempts; wait a few minutes", status=429)
        if username != self.config.owner_username or not self._check_password(password):
            with self._lock:
                self._failed_attempts.append(now)
            raise AuthError("Invalid credentials", status=401)

        token = secrets.token_urlsafe(_TOKEN_BYTES)
        issued = utcnow()
        expires = issued + timedelta(hours=self.config.auth_session_ttl_hours)
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO auth_sessions (token_hash, user_id, created_at, expires_at) VALUES (?, 1, ?, ?)",
                (_hash_token(token), iso(issued), iso(expires)),
            )
        return token

    def _check_password(self, password: str) -> bool:
        with self.db.read() as conn:
            row = conn.execute("SELECT password_hash FROM users WHERE id = 1").fetchone()
        if row is None:
            return False
        return verify_password(password, row["password_hash"])

    # ---------------------------------------------------------------- verify
    def verify_token(self, token: str) -> bool:
        """True if the token maps to a live, unexpired auth session."""
        if not token:
            return False
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT expires_at FROM auth_sessions WHERE token_hash = ?",
                (_hash_token(token),),
            ).fetchone()
        if row is None:
            return False
        if parse_iso(row["expires_at"]) < utcnow():
            self.logout(token)
            return False
        return True

    def logout(self, token: str) -> None:
        with self.db.tx() as conn:
            conn.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (_hash_token(token),))

    def purge_expired(self) -> int:
        with self.db.tx() as conn:
            cur = conn.execute(
                "DELETE FROM auth_sessions WHERE expires_at < ?", (iso_now(),)
            )
            return cur.rowcount



