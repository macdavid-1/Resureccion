"""Tests for the model client (using an injected mock transport)."""
from __future__ import annotations

import json

import httpx
import pytest

from app.config import Config
from app.model_client import (
    ModelClient,
    ModelConfigError,
    ModelContractError,
    ModelError,
)


def _config(**overrides) -> Config:
    cfg = Config()
    cfg.model_api_key = "test-key"
    cfg.model_base_url = "https://fake.invalid/api/v1"
    cfg.model_name = "test-model"
    cfg.model_timeout_seconds = 5
    cfg.model_vision_enabled = False  # opt in per-test
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _response(content: dict) -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"content": json.dumps(content)}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.001},
    })


@pytest.mark.asyncio
async def test_unconfigured_client_raises_model_config_error():
    cfg = _config()
    cfg.model_api_key = ""
    client = ModelClient(cfg)
    with pytest.raises(ModelConfigError):
        await client.call("s1", system="sys", user="usr")


@pytest.mark.asyncio
async def test_successful_call_returns_parsed_json():
    cfg = _config()
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.content)
        return _response({"signals": [], "observations": []})

    client = ModelClient(cfg, transport=_transport(handler))
    reply = await client.call(
        "s1", system="sys", user="usr", expect_fields=("signals", "observations"),
    )
    assert reply.content == {"signals": [], "observations": []}
    assert seen["url"].startswith("https://fake.invalid/api/v1/chat/completions")
    assert seen["auth"] == "Bearer test-key"
    assert seen["body"]["model"] == "test-model"
    assert reply.usage.calls == 1
    assert reply.usage.prompt_tokens == 10
    await client.aclose()


@pytest.mark.asyncio
async def test_missing_expected_fields_raises_contract_error_no_retry():
    cfg = _config()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _response({"wrong": 1})

    client = ModelClient(cfg, transport=_transport(handler))
    with pytest.raises(ModelContractError) as exc_info:
        await client.call("s1", system="s", user="u", expect_fields=("signals",))
    assert exc_info.value.missing == ["signals"]
    assert calls["n"] == 1  # contract errors must NOT be retried
    await client.aclose()


@pytest.mark.asyncio
async def test_4xx_surfaces_immediately_without_retry():
    cfg = _config()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, text="unauthorized")

    client = ModelClient(cfg, transport=_transport(handler))
    with pytest.raises(ModelError) as exc_info:
        await client.call("s1", system="s", user="u")
    assert "401" in str(exc_info.value)
    assert calls["n"] == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_json_in_code_fence_is_extracted():
    cfg = _config()

    def handler(request: httpx.Request) -> httpx.Response:
        fenced = "```json\n" + json.dumps({"signals": [1]}) + "\n```"
        return httpx.Response(200, json={"choices": [{"message": {"content": fenced}}]})

    client = ModelClient(cfg, transport=_transport(handler))
    reply = await client.call("s1", system="s", user="u")
    assert reply.content == {"signals": [1]}
    await client.aclose()


@pytest.mark.asyncio
async def test_vision_screenshot_attached_as_image_part():
    cfg = _config(model_vision_enabled=True)
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _response({"ok": True})

    client = ModelClient(cfg, transport=_transport(handler))
    png = b"\x89PNG-fake-bytes"
    await client.call("s1", system="s", user="analyze", screenshot_png=png)
    content = seen["body"]["messages"][1]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert content[0]["text"] == "analyze"
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    # base64 payload must decode back to the original bytes
    import base64

    b64 = content[1]["image_url"]["url"].split(",", 1)[1]
    assert base64.b64decode(b64) == png
    await client.aclose()


@pytest.mark.asyncio
async def test_vision_disabled_sends_plain_text():
    cfg = _config(model_vision_enabled=False)
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _response({"ok": True})

    client = ModelClient(cfg, transport=_transport(handler))
    await client.call("s1", system="s", user="analyze", screenshot_png=b"png")
    content = seen["body"]["messages"][1]["content"]
    assert content == "analyze"
    await client.aclose()


@pytest.mark.asyncio
async def test_no_screenshot_sends_plain_text_even_with_vision_on():
    cfg = _config(model_vision_enabled=True)
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _response({"ok": True})

    client = ModelClient(cfg, transport=_transport(handler))
    await client.call("s1", system="s", user="analyze")
    assert seen["body"]["messages"][1]["content"] == "analyze"
    await client.aclose()


def test_deepinfra_defaults():
    """Defaults must target GLM-5.3-Flash on DeepInfra."""
    from app.model_client import DEFAULT_BASE_URL, DEFAULT_MODEL

    assert DEFAULT_BASE_URL == "https://api.deepinfra.com/v1/openai"
    assert DEFAULT_MODEL == "zai-org/GLM-5.3-Flash"
    cfg = Config()
    assert cfg.model_base_url == DEFAULT_BASE_URL
    assert cfg.model_name == DEFAULT_MODEL
    assert cfg.model_vision_enabled is True


@pytest.mark.asyncio
async def test_usage_accounting_accumulates():
    cfg = _config()

    def handler(request: httpx.Request) -> httpx.Response:
        return _response({"ok": True})

    client = ModelClient(cfg, transport=_transport(handler))
    await client.call("s1", system="s", user="u")
    await client.call("s1", system="s", user="u")
    d = client.usage_dict()
    assert d["calls"] == 2
    assert d["prompt_tokens"] == 20
    assert d["configured"] is True
    assert d["model"] == "test-model"
    await client.aclose()
