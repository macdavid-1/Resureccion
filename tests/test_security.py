"""Tests for single-user authentication."""
from __future__ import annotations

import pytest

from app.events import EventLog
from app.security import AuthError, AuthService, hash_password, verify_password


@pytest.fixture()
def auth(db, config) -> AuthService:
    config.owner_password_hash = hash_password("correct horse")
    service = AuthService(db, config)
    service.ensure_user()
    return service


def test_password_hash_roundtrip() -> None:
    h = hash_password("s3cret")
    assert verify_password("s3cret", h)
    assert not verify_password("wrong", h)
    assert not verify_password("s3cret", "garbage")


def test_login_success_and_verify(auth) -> None:
    token = auth.login("owner", "correct horse")
    assert auth.verify_token(token)
    assert not auth.verify_token("bogus-token")


def test_login_wrong_password(auth) -> None:
    with pytest.raises(AuthError):
        auth.login("owner", "wrong")


def test_login_unknown_user(auth) -> None:
    with pytest.raises(AuthError):
        auth.login("nobody", "correct horse")


def test_logout_invalidates_token(auth) -> None:
    token = auth.login("owner", "correct horse")
    assert auth.verify_token(token)
    auth.logout(token)
    assert not auth.verify_token(token)


def test_rate_limiting(auth) -> None:
    for _ in range(10):
        with pytest.raises(AuthError):
            auth.login("owner", "nope")
    with pytest.raises(AuthError) as exc:
        auth.login("owner", "correct horse")
    assert exc.value.status == 429


def test_ensure_user_is_idempotent(db, config) -> None:
    config.owner_password_hash = hash_password("pw-one")
    s1 = AuthService(db, config)
    s1.ensure_user()
    config.owner_password_hash = hash_password("pw-two")
    s2 = AuthService(db, config)
    s2.ensure_user()
    # Login must use the latest configured hash.
    token = s2.login("owner", "pw-two")
    assert s2.verify_token(token)


def test_expired_token_rejected(auth, db) -> None:
    from app.timeutil import iso, utcnow
    from datetime import timedelta

    token = auth.login("owner", "correct horse")
    with db.tx() as conn:
        past = utcnow() - timedelta(hours=1)
        conn.execute(
            "UPDATE auth_sessions SET expires_at = ?", (iso(past),)
        )
    assert not auth.verify_token(token)


class TestPlaintextPasswordEnv:
    """OWNER_PASSWORD — the .env line the owner actually edits.

    The app used to read only OWNER_PASSWORD_HASH, so a .env carrying the
    username and password (the natural thing to write) logged nobody in:
    every attempt 401'd and the UI showed a generic "Sign in required".
    """

    def test_password_env_derives_a_usable_hash(self, monkeypatch) -> None:
        from app.config import Config

        monkeypatch.setenv("OWNER_PASSWORD", "correct horse")
        monkeypatch.delenv("OWNER_PASSWORD_HASH", raising=False)
        assert verify_password("correct horse", Config().owner_password_hash)

    def test_password_wins_over_a_stale_hash(self, monkeypatch) -> None:
        from app.config import Config

        stale = hash_password("old-generated")
        monkeypatch.setenv("OWNER_PASSWORD_HASH", stale)
        monkeypatch.setenv("OWNER_PASSWORD", "what I actually type")
        cfg = Config()
        assert cfg.owner_password_hash != stale
        assert verify_password("what I actually type", cfg.owner_password_hash)

    def test_end_to_end_login_with_env_credentials(self, db, monkeypatch) -> None:
        from app.config import Config

        monkeypatch.setenv("OWNER_USERNAME", "mac_david")
        monkeypatch.setenv("OWNER_PASSWORD", "correct horse")
        monkeypatch.delenv("OWNER_PASSWORD_HASH", raising=False)
        service = AuthService(db, Config())
        service.ensure_user()
        token = service.login("mac_david", "correct horse")
        assert service.verify_token(token)

    def test_malformed_stored_hash_fails_with_actionable_error(self, db, config) -> None:
        """Plaintext pasted into OWNER_PASSWORD_HASH: nothing can ever match
        it, so fail as a server misconfiguration (500), not as a wrong guess."""
        config.owner_password_hash = "correct horse"  # not a scrypt hash
        service = AuthService(db, config)
        service.ensure_user()
        with pytest.raises(AuthError) as exc:
            service.login("owner", "correct horse")
        assert exc.value.status == 500
        assert "OWNER_PASSWORD" in str(exc.value)


class TestCookieSecureFlag:
    """COOKIE_SECURE=true behind TLS (the HTTPS custom domain) must mark the
    session cookie Secure; the plain-http preview must stay cookie-compatible."""

    def test_default_is_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.config import Config

        monkeypatch.delenv("COOKIE_SECURE", raising=False)
        assert Config().cookie_secure is False

    def test_env_enables_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.config import Config

        monkeypatch.setenv("COOKIE_SECURE", "true")
        assert Config().cookie_secure is True

    def test_login_cookie_carries_the_flag(self, client) -> None:
        creds = {"username": "owner", "password": "hunter2"}

        plain = client.post("/api/auth/login", json=creds)
        assert plain.status_code == 200
        assert "secure" not in plain.headers.get("set-cookie", "").lower()

        client.app.state.config.cookie_secure = True
        tls = client.post("/api/auth/login", json=creds)
        assert tls.status_code == 200
        assert "secure" in tls.headers.get("set-cookie", "").lower()
