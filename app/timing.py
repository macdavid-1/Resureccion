"""Timing instrumentation + intelligent stuck detection.

Resurrección sessions run for six hours or more unattended. The agent must
KNOW how long everything takes — and when it has been stuck too long — so
the runner can recover, retry, change approach, or skip with a limitation
instead of hanging forever.

Design:
- TaskType-specific timeouts (navigation != model call != phase), never one
  global timeout.
- `TimingTracker` records start/end/duration/wait per task durably
  (task_timings table) so timing survives restarts.
- `StuckDetector` turns elapsed time into a verdict: ok / soft (warn +
  consider recovery) / hard (abort the task, record a limitation).
- The runner feeds timing digests to the model so the agent knows it has
  spent too long on something and can change approach.

Everything here is deterministic; no model involvement.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app.db import Database
from app.timeutil import iso_now


class TaskType:
    """Task categories, each with its own timeout policy."""

    PAGE_NAVIGATION = "page_navigation"
    MODEL_CALL = "model_call"
    PHASE = "phase"
    TASK = "task"
    VERIFICATION = "verification"
    RETRY_WAIT = "retry_wait"


# Timeout policy per task type (seconds). Tuned per type, not one global
# number: a page load that takes 90s is stuck, a model call that takes 90s
# is normal reasoning.
_TIMEOUTS: dict[str, dict[str, float]] = {
    TaskType.PAGE_NAVIGATION: {"soft": 45.0, "hard": 90.0},
    TaskType.MODEL_CALL: {"soft": 120.0, "hard": 300.0},
    TaskType.PHASE: {"soft": 1500.0, "hard": 3600.0},
    TaskType.TASK: {"soft": 240.0, "hard": 600.0},
    TaskType.VERIFICATION: {"soft": 300.0, "hard": 900.0},
    TaskType.RETRY_WAIT: {"soft": 30.0, "hard": 120.0},
}


def timeout_for(task_type: str) -> dict[str, float]:
    return _TIMEOUTS.get(task_type, _TIMEOUTS[TaskType.TASK])


@dataclass
class StuckVerdict:
    task_key: str
    task_type: str
    elapsed_seconds: float
    level: str                 # "ok" | "soft" | "hard"
    timeout_seconds: float
    advice: str                # what the runner should do

    @property
    def stuck(self) -> bool:
        return self.level != "ok"

    @property
    def hard_stuck(self) -> bool:
        return self.level == "hard"

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_key": self.task_key,
            "task_type": self.task_type,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "level": self.level,
            "timeout_seconds": self.timeout_seconds,
            "advice": self.advice,
        }


_ADVICE = {
    "ok": "continue",
    "soft": "attempt recovery, then retry once",
    "hard": "abort task; retry once with a different approach, else skip with a recorded limitation",
}


class TimingTracker:
    """Durable per-task timing. One tracker per session."""

    def __init__(self, db: Database, session_id: str) -> None:
        self.db = db
        self.session_id = session_id
        self._running: dict[str, tuple[str, float, float]] = {}  # key -> (type, t0, wait_accum)

    # ------------------------------------------------------------------ record
    def start(self, task_key: str, task_type: str, *, wait_seconds: float = 0.0) -> None:
        self._running[task_key] = (task_type, time.monotonic(), max(0.0, wait_seconds))
        now = iso_now()
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO task_timings (session_id, task_key, task_type, started_at, outcome, attempts, wait_seconds) "
                "VALUES (?, ?, ?, ?, 'running', 1, ?)",
                (self.session_id, task_key, task_type, now, max(0.0, wait_seconds)),
            )

    def finish(self, task_key: str, *, outcome: str = "ok") -> float | None:
        """Record completion; returns the duration in seconds (None if unknown key)."""
        entry = self._running.pop(task_key, None)
        if entry is None:
            return None
        task_type, t0, wait = entry
        duration = time.monotonic() - t0
        now = iso_now()
        with self.db.tx() as conn:
            row = conn.execute(
                "SELECT id, attempts FROM task_timings WHERE session_id = ? AND task_key = ? AND outcome = 'running' "
                "ORDER BY id DESC LIMIT 1",
                (self.session_id, task_key),
            ).fetchone()
            if row is not None:
                conn.execute(
                    "UPDATE task_timings SET finished_at = ?, duration_seconds = ?, outcome = ?, "
                    "wait_seconds = wait_seconds + ? WHERE id = ?",
                    (now, round(duration, 3), outcome, wait, row["id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO task_timings (session_id, task_key, task_type, started_at, finished_at, "
                    "duration_seconds, outcome, attempts, wait_seconds) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
                    (self.session_id, task_key, task_type, now, now, round(duration, 3), outcome, wait),
                )
        return duration

    def mark_retry(self, task_key: str) -> None:
        """Note that this task is being retried (keeps the running row open)."""
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE task_timings SET attempts = attempts + 1 "
                "WHERE session_id = ? AND task_key = ? AND outcome = 'running'",
                (self.session_id, task_key),
            )

    # ------------------------------------------------------------------ inspect
    def check(self, task_key: str) -> StuckVerdict:
        """Is this running task stuck? Type-aware, never one global timeout."""
        entry = self._running.get(task_key)
        if entry is None:
            return StuckVerdict(task_key, TaskType.TASK, 0.0, "ok", 0.0, _ADVICE["ok"])
        task_type, t0, _wait = entry
        elapsed = time.monotonic() - t0
        policy = timeout_for(task_type)
        if elapsed >= policy["hard"]:
            return StuckVerdict(task_key, task_type, elapsed, "hard", policy["hard"], _ADVICE["hard"])
        if elapsed >= policy["soft"]:
            return StuckVerdict(task_key, task_type, elapsed, "soft", policy["soft"], _ADVICE["soft"])
        return StuckVerdict(task_key, task_type, elapsed, "ok", policy["soft"], _ADVICE["ok"])

    def running_keys(self) -> list[str]:
        return list(self._running.keys())

    def abandon(self, task_key: str, *, outcome: str = "abandoned") -> None:
        self.finish(task_key, outcome=outcome)

    # ------------------------------------------------------------------ digest
    def session_summary(self, *, limit: int = 200) -> dict[str, Any]:
        """Aggregate timing digest for monitoring and the model prompt."""
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT task_key, task_type, duration_seconds, wait_seconds, outcome, attempts FROM task_timings "
                "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (self.session_id, limit),
            ).fetchall()
        by_type: dict[str, dict[str, float]] = {}
        slowest: list[dict[str, Any]] = []
        waiting_total = 0.0
        for r in rows:
            t = r["task_type"]
            d = by_type.setdefault(t, {"count": 0, "total_seconds": 0.0, "retries": 0})
            d["count"] += 1
            d["total_seconds"] += r["duration_seconds"] or 0.0
            d["retries"] += max(0, (r["attempts"] or 1) - 1)
            waiting_total += r["wait_seconds"] or 0.0
            if r["duration_seconds"] is not None:
                slowest.append(
                    {"task_key": r["task_key"], "type": t,
                     "seconds": round(r["duration_seconds"], 1), "outcome": r["outcome"]}
                )
        for t in by_type:
            by_type[t]["total_seconds"] = round(by_type[t]["total_seconds"], 1)
        slowest.sort(key=lambda x: -x["seconds"])
        failed = sum(1 for s in slowest if s["outcome"] not in ("ok", "running"))
        return {
            "by_type": by_type,
            "wait_seconds_total": round(waiting_total, 1),
            "slowest": slowest[:5],
            "failed_recent": failed,
        }

    def since_last_progress_seconds(self) -> float:
        """Seconds since the newest finished task — the 'no meaningful
        progress' clock the runner pivots on."""
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT started_at FROM task_timings WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                (self.session_id,),
            ).fetchone()
        if row is None:
            return 0.0
        from datetime import datetime

        try:
            started = datetime.fromisoformat(row["started_at"])
        except ValueError:
            return 0.0
        return max(0.0, (datetime.now(started.tzinfo) - started).total_seconds())


def model_timing_digest(tracker: TimingTracker) -> dict[str, Any]:
    """Compact timing info embedded in prompts: the agent must know how long
    things are taking and whether it is burning time without progress."""
    s = tracker.session_summary()
    return {
        "task_counts_by_type": {k: v["count"] for k, v in s["by_type"].items()},
        "wait_seconds_total": s["wait_seconds_total"],
        "slowest_recent": s["slowest"][:3],
        "seconds_since_last_activity": round(tracker.since_last_progress_seconds(), 1),
    }
