"""Reasoning capture: every provider spelling reaches the owner's CoT.

DeepInfra documents `reasoning_content`; OpenRouter uses `reasoning` or
`reasoning_details` (list of {text}); a vLLM-hosted reasoner without a
reasoning parser leaves ```-think blocks inside the answer text. Whichever
shape arrives, the owner's thought row must end up holding the real
reasoning — and when thinking is ON but nothing reasoning-shaped ever
arrives, the row must explain that instead of showing a bare placeholder.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.model_client import ModelClient
from app.sessions import SessionStore
from app.thoughts import STATUS_DONE, ThoughtStore


def _cfg(config):
    config.model_api_key = "test-key"
    config.model_base_url = "https://fake.invalid/api/v1"
    config.model_name = "test-model"
    config.model_timeout_seconds = 5
    config.model_vision_enabled = False
    return config


def _sse(*chunks) -> httpx.Response:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=body.encode(),
    )


def _plain(message: dict) -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": message}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3},
    })


def _sid(db, config) -> str:
    return SessionStore(db, config).create(mode="prompt", prompt="x").id


@pytest.mark.asyncio
async def test_stream_reasoning_string_and_list_shapes_reach_the_thought(db, config):
    """`delta.reasoning` as a string AND as a list of {text}/{content} parts
    (OpenRouter shape) both feed the live thought."""
    cfg = _cfg(config)
    sid = _sid(db, cfg)
    store = ThoughtStore(db)

    def handler(request: httpx.Request) -> httpx.Response:
        return _sse(
            {"choices": [{"delta": {"reasoning": "checking "}}]},
            {"choices": [{"delta": {"reasoning": [
                {"text": "competition"}, {"content": " levels"},
            ]}}]},
            {"choices": [{"delta": {"content": '{"signals": []}'}}],
             "usage": {"prompt_tokens": 11, "completion_tokens": 5}},
        )

    client = ModelClient(cfg, thoughts=store, transport=httpx.MockTransport(handler))
    reply = await client.call(sid, system="s", user="u", label="Phase 1 · step 1")

    assert reply.content == {"signals": []}
    thought = store.get(reply.thought_id)
    assert thought is not None and thought.status == STATUS_DONE
    assert thought.detail == "checking competition levels"
    await client.aclose()


@pytest.mark.asyncio
async def test_plain_response_reasoning_details_are_captured(db, config):
    """The non-streamed path reads `reasoning_details` (list of {text}) too."""
    cfg = _cfg(config)
    cfg.model_stream_enabled = False
    sid = _sid(db, cfg)
    store = ThoughtStore(db)

    def handler(request: httpx.Request) -> httpx.Response:
        return _plain({
            "content": json.dumps({"ok": True}),
            "reasoning_details": [{"type": "text", "text": "thinking it through"}],
        })

    client = ModelClient(cfg, thoughts=store, transport=httpx.MockTransport(handler))
    reply = await client.call(sid, system="s", user="u")

    assert reply.content == {"ok": True}
    thought = store.get(reply.thought_id)
    assert thought is not None
    assert thought.detail == "thinking it through"
    await client.aclose()


@pytest.mark.asyncio
async def test_think_tags_recovered_as_reasoning_and_kept_out_of_json(db, config):
    """A reasoner whose thinking the server never split out keeps its think
    block inside the answer: the block becomes the thought's detail and the
    JSON still parses from what remains."""
    cfg = _cfg(config)
    cfg.model_stream_enabled = False
    sid = _sid(db, cfg)
    store = ThoughtStore(db)
    raw = (
        "The user wants space books for kids.\n"
        "<" + "think>\n"
        "Let me weigh demand versus saturation before answering.\n"
        "</" + "think>\n\n"
        '{"signals": [{"topic": "space"}], "observations": []}'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return _plain({"content": raw})

    client = ModelClient(cfg, thoughts=store, transport=httpx.MockTransport(handler))
    reply = await client.call(
        sid, system="s", user="u", expect_fields=("signals", "observations"),
    )

    assert reply.content == {"signals": [{"topic": "space"}], "observations": []}
    thought = store.get(reply.thought_id)
    assert thought is not None
    assert "weigh demand versus saturation" in thought.detail
    assert "think" not in thought.detail  # tags never leak to the owner
    await client.aclose()


@pytest.mark.asyncio
async def test_completed_call_with_no_reasoning_explains_itself(db, config):
    """Thinking is ON but the provider exposed nothing: the row must say so,
    not leave the owner with '(no reasoning recorded for this call)'."""
    cfg = _cfg(config)
    cfg.model_stream_enabled = False
    sid = _sid(db, cfg)
    store = ThoughtStore(db)

    def handler(request: httpx.Request) -> httpx.Response:
        return _plain({"content": json.dumps({"ok": True})})

    client = ModelClient(cfg, thoughts=store, transport=httpx.MockTransport(handler))
    reply = await client.call(sid, system="s", user="u")

    thought = store.get(reply.thought_id)
    assert thought is not None
    assert "no reasoning text" in thought.detail.lower()
    await client.aclose()


@pytest.mark.asyncio
async def test_thinking_disabled_records_no_synthetic_note(db, config):
    """With MODEL_THINKING=false nothing reasoning-shaped is expected, so the
    thought stays empty and the UI shows its normal placeholder."""
    cfg = _cfg(config)
    cfg.model_stream_enabled = False
    cfg.model_thinking_enabled = False
    sid = _sid(db, cfg)
    store = ThoughtStore(db)

    def handler(request: httpx.Request) -> httpx.Response:
        return _plain({"content": json.dumps({"ok": True})})

    client = ModelClient(cfg, thoughts=store, transport=httpx.MockTransport(handler))
    reply = await client.call(sid, system="s", user="u")

    thought = store.get(reply.thought_id)
    assert thought is not None
    assert thought.detail == ""
    await client.aclose()
