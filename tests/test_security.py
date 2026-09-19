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
