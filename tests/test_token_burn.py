"""Output-token burn: what a step costs, and how it is counted.

A run went from ~1.4M output tokens over nearly five hours to ~22M over three,
while input stayed the same. Two independent reasons, both fixed here:

1. EVERY step owed the phase's whole analysis. An explore step only dispatches
   browser actions, yet the output contract was required on it too, so the
   model rewrote its entire findings document (and re-reasoned about it) on
   every single step of every phase. Now an explore step owes actions + a
   one-line note, and the analysis is written once on the committing step.

2. Usage was accounted by adding the CLIENT's cumulative total to a stored
   snapshot on every call. That is only correct while both sides start at the
   same zero; a resumed run, a re-prompt or a rebuilt client makes the stored
   total get added again and again. Accounting is now per call: each reply
   carries its own counts and they are added exactly once.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.config import Config
from app.methodology import P1_DISCOVERY, PhaseTiming
from app.model_client import ModelClient, ModelUsage
from app.prompts import build_user_prompt
from app.recovery import CheckpointStore
from app.research_runner import ResearchRunner


def _config(**overrides: Any) -> Config:
    cfg = Config()
    cfg.model_api_key = "test-key"
    cfg.model_base_url = "https://fake.invalid/api/v1"
    cfg.model_name = "test-model"
    cfg.model_timeout_seconds = 5
    cfg.model_vision_enabled = False
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _reply(usage: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"content": json.dumps({"signals": []})}}],
        "usage": usage,
    })


# ------------------------------------------------------------------- prompt
def _prompt() -> str:
    return build_user_prompt(
        P1_DISCOVERY,
        mode="auto",
        objective="find niches",
        prompt_text="",
        marketplaces=["us"],
        marketplace_mode="auto",
        evidence_digest=[],
        candidates_digest=[],
        prior_phase_outputs={},
        timing=PhaseTiming(phase=P1_DISCOVERY, started_at="now"),
    )


def test_an_explore_step_is_asked_for_actions_only() -> None:
    """The cheap shape is spelled out, and re-emitting the analysis is
    explicitly forbidden — that rewrite is the output-token sink."""
    p = _prompt()
    assert "(a) EXPLORE" in p
    assert "(b) COMMIT" in p
    assert "do NOT repeat the analysis here" in p
    # The analysis contract is presented as the COMMIT shape, not every step.
    commit_at = p.index("(b) COMMIT")
    assert "signals" in p[commit_at:]


# -------------------------------------------------------------------- client
@pytest.mark.asyncio
async def test_each_reply_carries_its_own_usage_not_the_running_total() -> None:
    cfg = _config()
    bodies = [
        {"prompt_tokens": 100, "completion_tokens": 10, "cost": 0.001},
        {"prompt_tokens": 120, "completion_tokens": 20, "cost": 0.002},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return _reply(bodies.pop(0))

    client = ModelClient(cfg, transport=httpx.MockTransport(handler))
    first = await client.call("s1", system="s", user="u")
    second = await client.call("s1", system="s", user="u")
    assert first.call_usage.completion_tokens == 10
    assert second.call_usage.completion_tokens == 20
    # The cumulative view is still cumulative (other callers rely on it).
    assert second.usage.completion_tokens == 30


@pytest.mark.asyncio
async def test_a_reported_count_above_the_cap_is_surfaced_in_the_log() -> None:
    """A provider that claims more tokens than the request allowed is a
    finding, not something to quietly sum into the owner's totals."""
    cfg = _config(model_max_tokens=100)

    def handler(request: httpx.Request) -> httpx.Response:
        return _reply({"prompt_tokens": 50, "completion_tokens": 7_000, "cost": 0.0})

    events: list[tuple[str, dict[str, Any]]] = []

    class Events:
        def append(self, session_id: str, *, level: str = "info", actor: str = "",
                   action: str = "", detail: dict | None = None) -> None:
            events.append((action, detail or {}))

    client = ModelClient(cfg, events=Events(), transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    reply = await client.call("s1", system="s", user="u")
    assert reply.call_usage.completion_tokens == 7_000
    assert reply.usage.implausible_calls == 1
    assert [a for a, _ in events if a == "model_usage_over_limit"]
    detail = next(d for a, d in events if a == "model_usage_over_limit")
    assert detail["completion_tokens"] == 7_000 and detail["max_tokens"] == 100


def test_truncated_answers_no_longer_get_an_unbounded_retry_budget() -> None:
    """The ceiling for a retried cut-off answer is a sane 32k, not 64k, and the
    retry asks for a shorter answer instead of only more room."""
    from app import model_client

    assert model_client.MAX_TOKENS_CEILING == 32_768


# ------------------------------------------------------------------ accounting
def _runner(db, **attrs: Any) -> Any:
    runner = SimpleNamespace(
        checkpoints=CheckpointStore(db),
        _usage_prior={},
        _usage_prior_seen={},
    )
    for k, v in attrs.items():
        setattr(runner, k, v)
    return runner


def _session(db) -> str:
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO research_sessions (id, name, status, mode, created_at, updated_at,"
            " last_activity_at) VALUES ('s1', 'n', 'running', 'auto', '2025-01-01',"
            " '2025-01-01', '2025-01-01')"
        )
    return "s1"


def _call(prompt: int, completion: int) -> Any:
    return SimpleNamespace(
        call_usage=ModelUsage(calls=1, prompt_tokens=prompt, completion_tokens=completion),
        usage=ModelUsage(calls=1, prompt_tokens=prompt, completion_tokens=completion),
    )


def _total(runner: Any, session_id: str) -> dict[str, Any]:
    cp = runner.checkpoints.get(session_id, "model_usage")
    return dict(cp.payload) if cp else {}


def test_usage_is_added_once_per_call(db) -> None:
    sid = _session(db)
    runner = _runner(db)
    ResearchRunner._record_model_usage(runner, sid, _call(100, 200))
    ResearchRunner._record_model_usage(runner, sid, _call(150, 300))
    total = _total(runner, sid)
    assert total["calls"] == 2
    assert total["prompt_tokens"] == 250
    assert total["completion_tokens"] == 500


def test_a_resumed_run_does_not_re_add_the_stored_totals(db) -> None:
    """The old code added the client's cumulative total to the snapshot on
    every call; a resume (fresh runner, fresh client) then re-added everything
    already stored. Output counts ran away in exactly this shape."""
    sid = _session(db)
    first = _runner(db)
    ResearchRunner._record_model_usage(first, sid, _call(100, 200))
    ResearchRunner._record_model_usage(first, sid, _call(100, 200))
    assert _total(first, sid)["completion_tokens"] == 400

    resumed = _runner(db)  # new process, loads the checkpoint as the baseline
    ResearchRunner._record_model_usage(resumed, sid, _call(100, 200))
    assert _total(resumed, sid)["completion_tokens"] == 600
    assert _total(resumed, sid)["prompt_tokens"] == 300


def test_legacy_cumulative_replies_are_turned_into_deltas(db) -> None:
    """Test doubles and older callers only carry the cumulative usage; it must
    be counted as a delta, and asking twice with the same total adds nothing."""
    sid = _session(db)
    runner = _runner(db)

    def cumulative(calls: int, prompt: int, completion: int) -> Any:
        u = ModelUsage(calls=calls, prompt_tokens=prompt, completion_tokens=completion)
        return SimpleNamespace(usage=u)

    ResearchRunner._record_model_usage(runner, sid, cumulative(1, 100, 200))
    assert _total(runner, sid)["completion_tokens"] == 200
    ResearchRunner._record_model_usage(runner, sid, cumulative(2, 200, 400))
    assert _total(runner, sid)["completion_tokens"] == 400
    # Same cumulative total seen again (a salvage re-report): nothing added.
    ResearchRunner._record_model_usage(runner, sid, cumulative(2, 200, 400))
    assert _total(runner, sid)["completion_tokens"] == 400
    assert _total(runner, sid)["calls"] == 2
