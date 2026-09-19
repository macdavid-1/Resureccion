"""Tests for checkpoints, error states, recovery, and jobs."""
from __future__ import annotations

import pytest

from app.events import EventLog
from app.jobs import JobError, JobOrchestrator
from app.recovery import CheckpointStore, ErrorStateStore, RecoveryManager
from app.sessions import SessionStore


def _mk_session(db, sid: str) -> None:
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO research_sessions (id, name, status, mode, created_at, updated_at, last_activity_at) VALUES (?, 'n', 'running', 'auto', '2025-01-01', '2025-01-01', '2025-01-01')",
            (sid,),
        )


@pytest.fixture()
def sessions(db, config) -> SessionStore:
    return SessionStore(db, config)


def test_checkpoint_upsert_and_latest(db) -> None:
    _mk_session(db, "s")
    cps = CheckpointStore(db)
    cps.save("s", "phase", {"step": 1})
    cps.save("s", "phase", {"step": 2})
    latest = cps.latest("s")
    assert latest.payload == {"step": 2}
    cps.save("s", "browser", {"url": "x"})
    assert cps.latest("s").key in ("phase", "browser")
    assert len(cps.list("s")) == 2
    cps.delete("s", "browser")
    assert len(cps.list("s")) == 1


def test_error_state_store(db) -> None:
    _mk_session(db, "s")
    errs = ErrorStateStore(db)
    errs.record("s", scope="browser", severity="warning", message="page timeout")
    errs.record("s", scope="model", severity="critical", message="quota exhausted", recoverable=False)
    rows = errs.unresolved("s")
    assert len(rows) == 2
    assert rows[0]["severity"] in ("critical", "warning")
    errs.resolve("s", rows[0]["id"])
    assert len(errs.unresolved("s")) == 1
    with pytest.raises(ValueError):
        errs.record("s", scope="x", severity="bogus", message="m")


def test_recovery_manager_marks_transient(sessions, db) -> None:
    events = EventLog(db)
    s1 = sessions.create(mode="auto")
    s2 = sessions.create(mode="auto")
    sessions.update(s1.id, status="running")
    sessions.update(s2.id, status="completed")
    cps = CheckpointStore(db)
    errs = ErrorStateStore(db)
    rm = RecoveryManager(db, sessions, cps, errs)
    recovered = rm.recover_all()
    assert s1.id in recovered
    assert s2.id not in recovered
    assert sessions.require(s1.id).status == "interrupted"
    assert sessions.require(s1.id).error
    assert len(errs.unresolved(s1.id)) == 1


def test_job_lifecycle(db, config, sessions) -> None:
    events = EventLog(db)
    errs = ErrorStateStore(db)
    orch = JobOrchestrator(db, config, sessions, events, errs)
    s = sessions.create(mode="auto")
    job = orch.create_job(s.id, kind="research_run")
    assert job.status == "pending" and job.attempt == 0
    job = orch.start_job(s.id, job.id)
    assert job.status == "running" and job.attempt == 1
    job = orch.complete_job(s.id, job.id)
    assert job.status == "succeeded" and job.finished_at


def test_job_failure_records_error(db, config, sessions) -> None:
    events = EventLog(db)
    errs = ErrorStateStore(db)
    orch = JobOrchestrator(db, config, sessions, events, errs)
    s = sessions.create(mode="auto")
    job = orch.create_job(s.id, kind="browse", max_attempts=1)
    orch.start_job(s.id, job.id)
    orch.complete_job(s.id, job.id, error="navigation timeout")
    assert orch.get(s.id, job.id).status == "failed"
    assert len(errs.unresolved(s.id)) == 1


def test_job_attempt_exhaustion(db, config, sessions) -> None:
    events = EventLog(db)
    errs = ErrorStateStore(db)
    orch = JobOrchestrator(db, config, sessions, events, errs)
    s = sessions.create(mode="auto")
    job = orch.create_job(s.id, kind="browse", max_attempts=2)
    orch.start_job(s.id, job.id)
    orch.complete_job(s.id, job.id, error="e1")
    orch.start_job(s.id, job.id)
    orch.complete_job(s.id, job.id, error="e2")
    with pytest.raises(JobError):
        orch.start_job(s.id, job.id)


def test_reconcile_requeues_running_jobs(db, config, sessions) -> None:
    events = EventLog(db)
    errs = ErrorStateStore(db)
    orch = JobOrchestrator(db, config, sessions, events, errs)
    s = sessions.create(mode="auto")
    job = orch.create_job(s.id, kind="browse")
    orch.start_job(s.id, job.id)
    counts = orch.reconcile_on_boot()
    assert counts["requeued_running_jobs"] >= 1
    assert orch.get(s.id, job.id).status == "pending"
