"""API tests for browser infrastructure routes."""
from __future__ import annotations

import pytest

from app.main import create_app
from app.security import hash_password


@pytest.fixture()
def client(monkeypatch, tmp_path):
    import os

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OWNER_USERNAME", "owner")
    monkeypatch.setenv("OWNER_PASSWORD_HASH", hash_password("hunter2"))
    monkeypatch.setenv("AUTH_SECRET", "unit-test-secret")
    monkeypatch.setenv("BROWSER_LOGIN_SECRET", "window-secret")
    from app import config as config_mod

    config_mod.get_config.cache_clear()
    from app import db as db_mod

    db_mod._db = None
    from fastapi.testclient import TestClient

    app = create_app()
    with TestClient(app) as c:
        yield c


def _h(client) -> dict:
    token = client.post(
        "/api/auth/login", json={"username": "owner", "password": "hunter2"}
    ).json()["token"]
    return {"X-Auth-Token": token}


def test_browser_status_requires_auth(client) -> None:
    assert client.get("/api/browser/status").status_code == 401


def test_browser_status_ok(client) -> None:
    headers = _h(client)
    res = client.get("/api/browser/status", headers=headers)
    assert res.status_code == 200
    st = res.json()["status"]
    assert st["running"] is False
    assert st["kdspy_loaded"] is False
    assert st["headless"] is True


def test_marketplaces_listed(client) -> None:
    headers = _h(client)
    res = client.get("/api/browser/marketplaces", headers=headers)
    assert res.status_code == 200
    codes = {m["code"] for m in res.json()["marketplaces"]}
    assert {"us", "uk", "de", "jp", "in"} <= codes


def test_marketplace_plan_resolution(client) -> None:
    headers = _h(client)
    res = client.post(
        "/api/browser/marketplaces/resolve", headers=headers, json={"marketplaces": ["de", "jp"]}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["mode"] == "explicit"
    assert [m["code"] for m in body["marketplaces"]] == ["de", "jp"]

    res2 = client.post("/api/browser/marketplaces/resolve", headers=headers, json={})
    assert res2.json()["mode"] == "auto"


def test_kdspy_state_endpoint(client) -> None:
    headers = _h(client)
    res = client.get("/api/browser/extensions/kdspy", headers=headers)
    assert res.status_code == 200
    assert res.json()["extension"]["status"] in ("not_configured", "installed", "validated", "failed")


def test_login_window_requires_secret(client) -> None:
    headers = _h(client)
    # Without the shared secret -> 403.
    res = client.post(
        "/api/browser/auth/amazon/login-window",
        headers=headers,
        json={"marketplace": "us"},
    )
    assert res.status_code == 403
    # With it, playwright is missing -> 503, but the window was created and
    # then cancelled; the error must not leak credentials.
    res2 = client.post(
        "/api/browser/auth/amazon/login-window",
        headers={**headers, "X-Login-Secret": "window-secret"},
        json={"marketplace": "us"},
    )
    assert res2.status_code in (503, 200)
    windows = client.get("/api/browser/auth/amazon/login-windows", headers=headers).json()["windows"]
    assert len(windows) >= 1
    assert windows[0]["result"].get("cookie") is None  # nothing sensitive


def test_amazon_auth_state_defaults_unknown(client) -> None:
    headers = _h(client)
    res = client.get("/api/browser/auth/amazon", headers=headers)
    assert res.status_code == 200
    assert res.json()["auth"]["status"] == "unknown"


def test_cookie_import_rejects_non_amazon(client) -> None:
    headers = _h(client)
    res = client.post(
        "/api/browser/auth/amazon/cookies",
        headers=headers,
        json={
            "marketplace": "us",
            "cookies": [{"name": "x", "value": "y", "domain": ".evil.com"}],
        },
    )
    assert res.status_code == 400
    assert "non-Amazon" in res.json()["detail"]


def test_cookie_import_rejects_lookalike_amazon_domains(client) -> None:
    """Regression: suffix 'endswith(amazon)' let notamazon.com / evil-amazon.com through."""
    from app.amazon_auth import AmazonAuthError, _validate_cookie_domains

    for bad in (".notamazon.com", ".evil-amazon.com", ".amazon.com.evil.io"):
        with pytest.raises(AmazonAuthError, match="non-Amazon"):
            _validate_cookie_domains([{"name": "x", "value": "y", "domain": bad}])
    # Genuine Amazon marketplace domains (exact or subdomain) pass.
    _validate_cookie_domains([{"name": "x", "value": "y", "domain": ".amazon.de"}])
    _validate_cookie_domains([{"name": "x", "value": "y", "domain": "amazon.co.uk"}])
    with pytest.raises(AmazonAuthError, match="malformed"):
        _validate_cookie_domains([{"domain": ".amazon.com"}])


def test_evidence_endpoint_scoped_to_session(client) -> None:
    headers = _h(client)
    sid = client.post(
        "/api/sessions", headers=headers, json={"mode": "auto"}
    ).json()["session"]["id"]
    res = client.get(f"/api/browser/evidence?session_id={sid}", headers=headers)
    assert res.status_code == 200
    assert res.json()["evidence"] == []
    res2 = client.get("/api/browser/evidence?session_id=missing", headers=headers)
    assert res2.status_code == 404
