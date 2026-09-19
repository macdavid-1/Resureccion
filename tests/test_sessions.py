"""Tests for research session lifecycle and modes."""
from __future__ import annotations

import pytest

from app.sessions import MODE_AUTO, MODE_KEYWORDS, MODE_PROMPT, SessionStore


@pytest.fixture()
def store(db, config) -> SessionStore:
    return SessionStore(db, config)


def test_create_prompt_mode(store) -> None:
    s = store.create(mode=MODE_PROMPT, prompt="Research low-content niches in pet journals")
    assert s.status == "draft"
    assert s.mode == MODE_PROMPT
    assert s.phase == "initializing"
    assert s.progress == 0.0
    assert s.name  # auto-generated


def test_create_keywords_mode(store) -> None:
    s = store.create(mode=MODE_KEYWORDS, prompt="swimming, triathlon, habit tracker")
    assert s.mode == MODE_KEYWORDS


def test_create_auto_mode_rejects_prompt(store) -> None:
    with pytest.raises(Exception):
        store.create(mode=MODE_AUTO, prompt="should not be allowed")
    s = store.create(mode=MODE_AUTO)
    assert s.mode == MODE_AUTO


def test_prompt_mode_requires_text(store) -> None:
    with pytest.raises(Exception):
        store.create(mode=MODE_PROMPT, prompt="")


def test_keywords_mode_requires_text_or_images(store) -> None:
    with pytest.raises(Exception):
        store.create(mode=MODE_KEYWORDS, prompt="")
    s = store.create(mode=MODE_KEYWORDS, prompt="", has_images=True)
    assert s.mode == MODE_KEYWORDS


def test_invalid_mode(store) -> None:
    with pytest.raises(Exception):
        store.create(mode="bogus")


def test_marketplace_restrictions(store) -> None:
    s = store.create(mode=MODE_PROMPT, prompt="p", marketplaces=["amazon.com", "amazon.de"])
    assert s.marketplaces == ["amazon.com", "amazon.de"]


def test_session_cap(store, config) -> None:
    config.max_sessions = 2
    store.create(mode=MODE_AUTO)
    store.create(mode=MODE_AUTO)
    with pytest.raises(Exception):
        store.create(mode=MODE_AUTO)
    # Terminal sessions free capacity.
    s = store.list()[0]
    store.update(s.id, status="completed")
    store.create(mode=MODE_AUTO)


def test_status_transition_validation(store) -> None:
    s = store.create(mode=MODE_AUTO)
    updated = store.update(s.id, status="queued")
    assert updated.status == "queued"
    with pytest.raises(Exception):
        store.update(s.id, status="completed-invalid")


def test_cannot_run_terminal_session(store) -> None:
    s = store.create(mode=MODE_AUTO)
    store.update(s.id, status="cancelled")
    with pytest.raises(Exception):
        store.update(s.id, status="running")


def test_counts_and_touch(store) -> None:
    s = store.create(mode=MODE_AUTO)
    store.touch(s.id)
    counts = store.counts(s.id)
    assert counts == {"discovered": 0, "rejected": 0, "verifying": 0, "verified": 0}


def test_bump_resume(store) -> None:
    s = store.create(mode=MODE_AUTO)
    assert s.resume_count == 0
    store.bump_resume(s.id)
    store.bump_resume(s.id)
    assert store.resume_count(s.id) == 2


def test_mark_interrupted_only_from_transient(store) -> None:
    s = store.create(mode=MODE_AUTO)
    store.update(s.id, status="running")
    store.mark_interrupted(s.id, "crash")
    assert store.require(s.id).status == "interrupted"
    # completed sessions are untouched
    store.update(s.id, status="completed")
    store.mark_interrupted(s.id, "crash again")
    assert store.require(s.id).status == "completed"
