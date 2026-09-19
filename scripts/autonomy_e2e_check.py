"""E2E autonomy verification: pause/resume, restart recovery, timing, trace,
and a complete scripted research run exercising the full autonomy layer."""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("OWNER_USERNAME", "owner")

from app.autonomy import AutonomySupervisor  # noqa: E402
from app.config import Config  # noqa: E402
from app.db import Database  # noqa: E402
from app.events import EventLog  # noqa: E402
from app.jobs import JobOrchestrator  # noqa: E402
from app.recovery import CheckpointStore, ErrorStateStore  # noqa: E402
from app.research_data import CandidateStore  # noqa: E402
from app.sessions import SessionStore  # noqa: E402
from app.timing import TimingTracker  # noqa: E402
from app.trace import ActivityTrace  # noqa: E402


def main() -> int:
    import tests.test_research_runner as trr

    cfg = Config()
    cfg.data_dir = Path(tempfile.mkdtemp(prefix="resurrect_auto_e2e_"))
    cfg.db_path = cfg.data_dir / "e2e.db"
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    db.connect()

    events = EventLog(db)
    errors = ErrorStateStore(db)
    checkpoints = CheckpointStore(db)
    sessions = SessionStore(db, cfg)
    supervisor = AutonomySupervisor(
        db, cfg, sessions=sessions, events=events, errors=errors, checkpoints=checkpoints,
    )

    print("=== 1. full run with trace + timings ===")
    s = sessions.create(mode="prompt", prompt="research grief journals", objective="x")
    jobs = JobOrchestrator(db, cfg, sessions, events, errors)
    job = jobs.create_job(s.id, kind="research_run")
    supervisor.mark(s.id, "researching")

    model = trr.ScriptedModel(trr.PHASE_OUTPUTS)
    runner = trr.make_runner(db, cfg, model)
    asyncio.run(runner.run(s.id))
    sess = sessions.require(s.id)
    print("status:", sess.status, "| error:", sess.error)

    trace = ActivityTrace(db)
    entries = trace.recent(s.id, limit=100)
    print(f"trace entries: {len(entries)}")
    for e in entries[:8]:
        print(f"  [{e.kind}] {e.text[:100]}")

    timings = TimingTracker(db, s.id).session_summary()
    print("timing by type:", {k: v["count"] for k, v in timings["by_type"].items()})

    print("\n=== 2. simulated restart recovery ===")
    s2 = sessions.create(mode="prompt", prompt="research prayer journals", objective="y")
    jobs.create_job(s2.id, kind="research_run")
    # Simulate a session that died mid-phase with checkpoints present.
    checkpoints.save(s2.id, "phase_complete:opportunity_discovery", {"at": "t0"})
    checkpoints.save(s2.id, "heartbeat", {"phase": "aggressive_niching", "at": "t1"})
    sessions.update(s2.id, status="running")
    counts = supervisor.recover_on_boot()
    print("recovery counts:", counts)
    after = sessions.require(s2.id)
    print("s2 status after boot:", after.status, "| resume_requested:",
          checkpoints.get(s2.id, "resume_requested") is not None)

    print("\n=== 3. owner pause lands durable + resumes ===")
    s3 = sessions.create(mode="prompt", prompt="research divorce journals", objective="z")
    m3 = trr.ScriptedModel(trr.PHASE_OUTPUTS)
    r3 = trr.make_runner(db, cfg, m3)
    orig = m3.call

    async def call_then_pause(*a, **k):
        reply = await orig(*a, **k)
        r3.request_pause()
        return reply

    m3.call = call_then_pause  # type: ignore[method-assign]
    asyncio.run(r3.run(s3.id))
    print("s3 after pause:", sessions.require(s3.id).status,
          "| paused:owner checkpoint:", checkpoints.get(s3.id, "paused:owner") is not None)
    r3b = trr.make_runner(db, cfg, trr.ScriptedModel(trr.PHASE_OUTPUTS))
    asyncio.run(r3b.run(s3.id))
    print("s3 after resume:", sessions.require(s3.id).status)

    print("\n=== 4. candidates ===")
    for c in CandidateStore(db).list(s.id):
        print(f"  [{c.status}] {c.niche}")

    ok = (
        sess.status == "completed"
        and after.status == "interrupted"
        and sessions.require(s3.id).status == "completed"
        and len(entries) >= 5
        and timings["by_type"]
    )
    print("\nE2E RESULT:", "PASS" if ok else "FAIL")
    db.close()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
