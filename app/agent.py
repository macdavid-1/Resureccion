"""Agent state machine.

The agent's state is a first-class persisted entity, never an in-process
object, so a crash at any moment loses nothing. State transitions are
validated and journaled through the event log.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from app.db import Database
from app.events import EventLog
from app.timeutil import iso_now

# Agent lifecycle states. Transitions are constrained by TRANSITIONS.
AGENT_STATES = (
    "idle",
    "planning",
    "exploring",
    "observing",
    "analyzing",
    "verifying",
    "synthesizing",
    "reporting",
    "done",
    "waiting_recovery",
    "stopped",
    "failed",
)

TRANSITIONS: dict[str, set[str]] = {
    "idle": {"planning", "stopped"},
    # planning may jump straight to any working state: phases with distinct
    # agent_states start at different points (e.g. P2 starts at analyzing).
    "planning": {"exploring", "observing", "analyzing", "verifying", "synthesizing", "waiting_recovery", "stopped", "failed"},
    "exploring": {"observing", "analyzing", "planning", "verifying", "waiting_recovery", "stopped", "failed"},
    "observing": {"exploring", "analyzing", "verifying", "waiting_recovery", "stopped", "failed"},
    "analyzing": {"verifying", "synthesizing", "exploring", "observing", "planning", "waiting_recovery", "stopped", "failed"},
    "verifying": {"synthesizing", "analyzing", "observing", "exploring", "waiting_recovery", "stopped", "failed"},
    "synthesizing": {"reporting", "verifying", "planning", "exploring", "waiting_recovery", "stopped", "failed"},
    "reporting": {"done", "synthesizing", "planning", "stopped", "failed"},
    "done": {"planning", "stopped"},
    "waiting_recovery": {"planning", "exploring", "observing", "analyzing", "verifying", "synthesizing", "stopped", "failed"},
    "stopped": set(),
    "failed": set(),
}


class AgentStateError(Exception):
    pass


@dataclass
class AgentState:
    session_id: str
    state: str
    substate: str
    current_goal: str
    current_task: str
    iteration: int
    budget_used_seconds: float
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "state": self.state,
            "substate": self.substate,
            "current_goal": self.current_goal,
            "current_task": self.current_task,
            "iteration": self.iteration,
            "budget_used_seconds": self.budget_used_seconds,
            "updated_at": self.updated_at,
        }


class AgentStateStore:
    def __init__(self, db: Database, events: EventLog) -> None:
        self.db = db
        self.events = events

    def initialize(self, session_id: str) -> AgentState:
        with self.db.tx() as conn:
            conn.execute(
                """
                INSERT INTO agent_states (session_id, state, substate, current_goal, current_task, iteration, budget_used_seconds, updated_at)
                VALUES (?, 'idle', '', '', '', 0, 0.0, ?)
                ON CONFLICT(session_id) DO NOTHING
                """,
                (session_id, iso_now()),
            )
        return self.get(session_id)  # type: ignore[return-value]

    def get(self, session_id: str) -> AgentState | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM agent_states WHERE session_id = ?", (session_id,)
            ).fetchone()
        return self._row_to_state(row) if row else None

    def require(self, session_id: str) -> AgentState:
        s = self.get(session_id)
        if s is None:
            raise AgentStateError(f"agent state for session {session_id} not initialized")
        return s

    def transition(
        self,
        session_id: str,
        to_state: str,
        *,
        substate: str = "",
        goal: str | None = None,
        task: str | None = None,
    ) -> AgentState:
        current = self.require(session_id)
        if to_state not in AGENT_STATES:
            raise AgentStateError(f"unknown agent state {to_state!r}")
        allowed = TRANSITIONS.get(current.state, set())
        if to_state not in allowed and to_state != current.state:
            raise AgentStateError(
                f"invalid transition {current.state!r} -> {to_state!r}; allowed: {sorted(allowed)}"
            )
        with self.db.tx() as conn:
            conn.execute(
                """
                UPDATE agent_states
                SET state = ?, substate = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (to_state, substate, iso_now(), session_id),
            )
        self.events.append(
            session_id,
            level="info",
            actor="agent",
            action="agent_state_transition",
            detail={"from": current.state, "to": to_state, "substate": substate},
        )
        return self.require(session_id)

    def set_goal(self, session_id: str, goal: str, task: str = "") -> AgentState:
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE agent_states SET current_goal = ?, current_task = ?, updated_at = ? WHERE session_id = ?",
                (goal, task, iso_now(), session_id),
            )
        return self.require(session_id)

    def advance_iteration(self, session_id: str) -> AgentState:
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE agent_states SET iteration = iteration + 1, updated_at = ? WHERE session_id = ?",
                (iso_now(), session_id),
            )
        return self.require(session_id)

    def add_budget(self, session_id: str, seconds: float) -> AgentState:
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE agent_states SET budget_used_seconds = budget_used_seconds + ?, updated_at = ? WHERE session_id = ?",
                (max(0.0, seconds), iso_now(), session_id),
            )
        return self.require(session_id)

    def _row_to_state(self, row: sqlite3.Row) -> AgentState:
        return AgentState(
            session_id=row["session_id"],
            state=row["state"],
            substate=row["substate"],
            current_goal=row["current_goal"],
            current_task=row["current_task"],
            iteration=row["iteration"],
            budget_used_seconds=row["budget_used_seconds"],
            updated_at=row["updated_at"],
        )
