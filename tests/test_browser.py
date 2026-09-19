"""Tests for the browser session registry."""
from __future__ import annotations

import pytest

from app.browser import BrowserRegistry, BrowserRegistryError
from app.sessions import SessionStore


@pytest.fixture()
def sessions(db, config) -> SessionStore:
    return SessionStore(db, config)


@pytest.fixture()
def registry(db, sessions) -> BrowserRegistry:
    return BrowserRegistry(db, sessions)


def test_ensure_and_get(registry, sessions) -> None:
    s = sessions.create(mode="auto")
    b = registry.ensure(s.id)
    assert b.status == "none"
    again = registry.ensure(s.id)
    assert again.status == "none"


def test_status_flow(registry, sessions) -> None:
    s = sessions.create(mode="auto")
    registry.ensure(s.id)
    registry.set_status(s.id, "launching")
    b = registry.record_health(s.id)
    assert b.status == "ready"
    b = registry.set_status(s.id, "busy")
    assert b.status == "busy"
    registry.mark_closed(s.id)
    assert registry.require(s.id).status == "closed"


def test_invalid_status(registry, sessions) -> None:
    s = sessions.create(mode="auto")
    registry.ensure(s.id)
    with pytest.raises(BrowserRegistryError):
        registry.set_status(s.id, "on_fire")


def test_crash_counting(registry, sessions) -> None:
    s = sessions.create(mode="auto")
    registry.ensure(s.id)
    registry.record_crash(s.id, "renderer died")
    b = registry.record_crash(s.id, "again")
    assert b.crash_count == 2
    assert b.status == "crashed"
    assert registry.crashed_sessions() == [s.id]


def test_launch_config(registry, sessions) -> None:
    s = sessions.create(mode="auto")
    registry.ensure(s.id)
    b = registry.set_launch_config(s.id, {"headless": True, "ksspy_profile": "pro"})
    assert b.launch_config["headless"] is True


def test_requires_existing_session(registry, sessions) -> None:
    with pytest.raises(Exception):
        registry.ensure("nope")
