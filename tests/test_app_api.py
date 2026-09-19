"""End-to-end API tests using FastAPI TestClient."""
from __future__ import annotations

import pytest


def _login(client) -> dict:
    res = client.post("/api/auth/login", json={"username": "owner", "password": "hunter2"})
    assert res.status_code == 200
    return res.json()


def test_health(client) -> None:
    assert client.get("/api/health").status_code == 200


def test_unknown_session_returns_404_not_400(client) -> None:
    """Regression: unknown session ids must 404, not bubble up as 400."""
    headers = {"X-Auth-Token": _login(client)["token"]}
    for path in (
        "/api/sessions/does-not-exist",
        "/api/sessions/does-not-exist/candidates",
        "/api/sessions/does-not-exist/events",
        "/api/sessions/does-not-exist/reports",
        "/api/sessions/does-not-exist/exports",
    ):
        res = client.get(path, headers=headers)
        assert res.status_code == 404, path
    res = client.post("/api/sessions/does-not-exist/start", headers=headers)
    assert res.status_code == 404


def test_auth_required_for_sessions(client) -> None:
    res = client.get("/api/sessions")
    assert res.status_code == 401


def test_login_and_session_flow(client) -> None:
    data = _login(client)
    token = data["token"]
    headers = {"X-Auth-Token": token}

    res = client.get("/api/auth/me", headers=headers)
    assert res.status_code == 200
    assert res.json()["username"] == "owner"

    res = client.post(
        "/api/sessions",
        headers=headers,
        json={"mode": "prompt", "prompt": "research pet grief journals"},
    )
    assert res.status_code == 200
    session = res.json()["session"]
    assert session["status"] == "draft"
    assert session["agent"]["state"] == "idle"
    assert session["browser"]["status"] == "none"

    sid = session["id"]
    res = client.post(f"/api/sessions/{sid}/start", headers=headers)
    assert res.status_code == 200
    assert res.json()["session"]["status"] == "queued"

    res = client.get(f"/api/sessions/{sid}", headers=headers)
    assert res.status_code == 200
    body = res.json()["session"]
    assert body["counts"] == {"discovered": 0, "rejected": 0, "verifying": 0, "verified": 0}

    res = client.get(f"/api/sessions/{sid}/events", headers=headers)
    assert res.status_code == 200
    actions = [e["action"] for e in res.json()["events"]]
    assert "job_created" in actions


def test_create_session_validation(client) -> None:
    _login(client)
    token_header = {"X-Auth-Token": client.post(
        "/api/auth/login", json={"username": "owner", "password": "hunter2"}
    ).json()["token"]}
    res = client.post("/api/sessions", headers=token_header, json={"mode": "prompt", "prompt": ""})
    assert res.status_code == 400


def test_logout_invalidates(client) -> None:
    token = _login(client)["token"]
    headers = {"X-Auth-Token": token}
    assert client.get("/api/auth/me", headers=headers).status_code == 200
    client.post("/api/auth/logout", headers=headers)
    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_reports_and_exports_flow(client) -> None:
    headers = {"X-Auth-Token": _login(client)["token"]}
    sid = client.post(
        "/api/sessions", headers=headers, json={"mode": "auto"}
    ).json()["session"]["id"]

    res = client.post(
        f"/api/sessions/{sid}/reports",
        headers=headers,
        json={"kind": "intermediate", "title": "Mid-run findings", "body_markdown": "# Data"},
    )
    assert res.status_code == 200

    res = client.post(f"/api/sessions/{sid}/exports", headers=headers, json={"format": "json"})
    assert res.status_code == 200
    export = res.json()["export"]
    assert export["status"] == "succeeded"

    res = client.get(f"/api/sessions/{sid}/exports/{export['id']}/file", headers=headers)
    assert res.status_code == 200


def test_upload_roundtrip(client) -> None:
    headers = {"X-Auth-Token": _login(client)["token"]}
    sid = client.post(
        "/api/sessions", headers=headers, json={"mode": "keywords", "prompt": "cozy cats"}
    ).json()["session"]["id"]
    res = client.post(
        f"/api/sessions/{sid}/uploads",
        headers=headers,
        files={"file": ("cover.png", b"\x89PNG fake", "image/png")},
    )
    assert res.status_code == 200
    up = res.json()["upload"]
    res = client.get(f"/api/sessions/{sid}/uploads/{up['id']}/file", headers=headers)
    assert res.status_code == 200
    assert res.content == b"\x89PNG fake"
