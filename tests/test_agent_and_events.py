"""Tests for agent state machine and event log."""
from __future__ import annotations

import pytest

from app.agent import AgentStateError, AgentStateStore
from app.events import EventLog


@pytest.fixture()
def events(db) -> EventLog:
    return EventLog(db)


@pytest.fixture()
def agent(db, events) -> AgentStateStore:
    return AgentStateStore(db, events)


def test_initialize_is_idempotent(agent, db) -> None:
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO research_sessions (id, name, status, mode, created_at, updated_at, last_activity_at) VALUES ('s1', 'n', 'draft', 'auto', '2025-01-01', '2025-01-01', '2025-01-01')"
        )
    a1 = agent.initialize("s1")
    a2 = agent.initialize("s1")
    assert a1.state == "idle"
    assert a2.state == "idle"


def test_valid_transition(agent, db, events) -> None:
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO research_sessions (id, name, status, mode, created_at, updated_at, last_activity_at) VALUES ('s1', 'n', 'draft', 'auto', '2025-01-01', '2025-01-01', '2025-01-01')"
        )
    agent.initialize("s1")
    agent.transition("s1", "planning")
    agent.transition("s1", "exploring")
    assert agent.require("s1").state == "exploring"
    # transition was journaled
    evs = events.tail("s1")
    assert any(e.action == "agent_state_transition" for e in evs)


def test_invalid_transition(agent, db) -> None:
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO research_sessions (id, name, status, mode, created_at, updated_at, last_activity_at) VALUES ('s2', 'n', 'draft', 'auto', '2025-01-01', '2025-01-01', '2025-01-01')"
        )
    agent.initialize("s2")
    with pytest.raises(AgentStateError):
        agent.transition("s2", "reporting")  # idle -> reporting not allowed


def test_unknown_state(agent, db) -> None:
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO research_sessions (id, name, status, mode, created_at, updated_at, last_activity_at) VALUES ('s3', 'n', 'draft', 'auto', '2025-01-01', '2025-01-01', '2025-01-01')"
        )
    agent.initialize("s3")
    with pytest.raises(AgentStateError):
        agent.transition("s3", "sleeping")


def test_reporting_transitions_to_done(agent, db) -> None:
    """Regression: reporting must reach a real 'done' state, not a placeholder."""
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO research_sessions (id, name, status, mode, created_at, updated_at, last_activity_at) VALUES ('sr', 'n', 'draft', 'auto', '2025-01-01', '2025-01-01', '2025-01-01')"
        )
    agent.initialize("sr")
    for step in ("planning", "exploring", "analyzing", "verifying", "synthesizing", "reporting"):
        agent.transition("sr", step)
    agent.transition("sr", "done")
    assert agent.require("sr").state == "done"
    # 'done' is terminal-ish but allows planning a follow-up or stopping.
    agent.transition("sr", "planning")


def test_goal_and_iteration(agent, db) -> None:
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO research_sessions (id, name, status, mode, created_at, updated_at, last_activity_at) VALUES ('s4', 'n', 'draft', 'auto', '2025-01-01', '2025-01-01', '2025-01-01')"
        )
    agent.initialize("s4")
    agent.set_goal("s4", "find 5 niches", "scan KDSpy categories")
    agent.advance_iteration("s4")
    agent.add_budget("s4", 120.5)
    st = agent.require("s4")
    assert st.current_goal == "find 5 niches"
    assert st.current_task == "scan KDSpy categories"
    assert st.iteration == 1
    assert st.budget_used_seconds == 120.5


def test_event_log_validation(db) -> None:
    log = EventLog(db)
    with pytest.raises(ValueError):
        log.append("s", level="nope", action="x")
    with pytest.raises(ValueError):
        log.append("s", actor="nope", action="x")


def test_event_tail_and_errors(db) -> None:
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO research_sessions (id, name, status, mode, created_at, updated_at, last_activity_at) VALUES ('sx', 'n', 'draft', 'auto', '2025-01-01', '2025-01-01', '2025-01-01'), ('sy', 'n', 'draft', 'auto', '2025-01-01', '2025-01-01', '2025-01-01')"
        )
    log = EventLog(db)
    log.append("sx", action="a")
    log.append("sx", level="error", action="b")
    log.append("sy", action="c")
    assert len(log.tail("sx")) == 2
    errs = log.latest_errors("sx")
    assert len(errs) == 1 and errs[0].action == "b"
