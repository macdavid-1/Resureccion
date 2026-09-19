"""Tests for browser-layer durable stores."""
from __future__ import annotations

import pytest

from app.browser_store import (
    AUTH_CAPTCHA,
    AUTH_CONFIRMED,
    AUTH_LOGIN_REQUIRED,
    AUTH_UNKNOWN,
    BrowserAuthStore,
    BrowserEvidenceStore,
    EXT_INSTALLED,
    ExtensionStore,
    LoginWindowStore,
    WINDOW_COMPLETED,
    WINDOW_OPEN,
    WINDOW_PENDING,
)


def _mk_session(db, sid: str) -> None:
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO research_sessions (id, name, status, mode, created_at, updated_at, last_activity_at) VALUES (?, 'n', 'running', 'auto', '2025-01-01', '2025-01-01', '2025-01-01')",
            (sid,),
        )


def test_auth_state_upsert(db) -> None:
    store = BrowserAuthStore(db)
    store.set("amazon", "kdspy", AUTH_UNKNOWN)
    store.set("amazon", "kdspy", AUTH_CONFIRMED, {"checked_at_url": "https://amazon.com"})
    state = store.get("amazon", "kdspy")
    assert state.status == AUTH_CONFIRMED
    assert state.detail["checked_at_url"] == "https://amazon.com"
    assert len(store.list()) == 1
    with pytest.raises(ValueError):
        store.set("amazon", "kdspy", "bogus")


def test_auth_state_redacts_detail(db) -> None:
    store = BrowserAuthStore(db)
    # Even if something credential-shaped sneaks into detail, to_dict drops it.
    store.set("amazon", "kdspy", AUTH_LOGIN_REQUIRED, {"reason": "form", "cookie": "session=1"})
    d = store.get("amazon", "kdspy").to_dict()
    assert "cookie" not in d["detail"]
    assert d["detail"]["reason"] == "form"


def test_extension_store_upsert(db) -> None:
    store = ExtensionStore(db)
    store.upsert("kdspy", "kdspy", status=EXT_INSTALLED, version="4.0")
    store.upsert("kdspy", "kdspy", status=EXT_INSTALLED, version="4.1")
    assert store.get("kdspy", "kdspy").version == "4.1"
    assert len(store.list()) == 1
    with pytest.raises(ValueError):
        store.upsert("kdspy", "kdspy", status="half-broken")


def test_login_window_lifecycle(db) -> None:
    store = LoginWindowStore(db)
    w = store.create("amazon", "kdspy", ttl_seconds=900)
    assert w.status == WINDOW_PENDING
    w = store.set_status(w.id, WINDOW_OPEN)
    assert w.status == WINDOW_OPEN and w.closed_at is None
    w = store.set_status(w.id, WINDOW_COMPLETED, {"verified": True})
    assert w.status == WINDOW_COMPLETED and w.closed_at is not None
    with pytest.raises(ValueError):
        store.set_status(w.id, "teleported")


def test_login_window_expiry(db) -> None:
    from datetime import timedelta

    from app.timeutil import iso, utcnow

    store = LoginWindowStore(db)
    w = store.create("amazon", "kdspy", ttl_seconds=900)
    with db.tx() as conn:
        past = iso(utcnow() - timedelta(hours=2))
        conn.execute("UPDATE login_windows SET expires_at = ? WHERE id = ?", (past, w.id))
    assert store.expire_stale() == 1
    assert store.require(w.id).status == "expired"


def test_browser_evidence_store(db) -> None:
    _mk_session(db, "sess1")
    store = BrowserEvidenceStore(db)
    ev = store.create(
        session_id="sess1",
        marketplace="us",
        kind="search_results",
        url="https://amazon.com/s?k=x&sid=secret",
        title="Results",
        data={"result_count": 5, "results": []},
    )
    got = store.get(ev.id)
    assert got.marketplace == "us"
    assert got.data["result_count"] == 5
    assert store.count("sess1") == 1
    assert store.list("other") == []
