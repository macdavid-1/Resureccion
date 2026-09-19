"""Real LLM client for the research agent.

OpenAI-compatible chat-completions JSON client with native vision support.
Default endpoint is DeepInfra serving **GLM-5.3-Flash** (native vision +
strong reasoning + prompt caching): it reasons, orchestrates, reads the live
browser screenshot, and emits JSON. Any OpenAI-compatible endpoint works —
configured entirely via environment variables:

- MODEL_API_KEY         (required for research; absent -> model calls fail
                         with ModelConfigError)
- MODEL_BASE_URL        (default https://api.deepinfra.com/v1/openai)
- MODEL_NAME            (default zai-org/GLM-5.3-Flash)
- MODEL_MAX_TOKENS      (default 8192)
- MODEL_TEMPERATURE     (default 0.4)
- MODEL_VISION_ENABLED  (default true — attach latest screenshot per call)
- MODEL_VISION_MAX_BYTES (default 400000 — size guard for screenshots)

All model output is parsed as strict JSON. Retries use exponential backoff
on 429/5xx/network errors; 4xx errors surface immediately as ModelError.
Contract violations (missing required fields) raise ModelContractError
without retrying — the runner records them as recoverable errors.
Usage (tokens and cost, when the provider reports it) is tracked per call
and cumulatively for the process.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import Config
from app.events import EventLog
from app.redact import scrub_url

DEFAULT_BASE_URL = "https://api.deepinfra.com/v1/openai"
DEFAULT_MODEL = "zai-org/GLM-5.3-Flash"


class ModelConfigError(Exception):
    """MODEL_API_KEY missing or invalid configuration."""


class ModelError(Exception):
    """Model call failed after retries."""


class ModelContractError(ModelError):
    """Model returned JSON missing required fields (deterministic — not retried)."""

    def __init__(self, message: str, *, missing: list[str], raw: str = "") -> None:
        super().__init__(message)
        self.missing = missing
        self.raw = raw


@dataclass
class ModelUsage:
    calls: int = 0
    failed_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "failed_calls": self.failed_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": round(self.cost_usd, 6),
        }


@dataclass
class ModelReply:
    content: dict[str, Any]
    raw: str
    usage: ModelUsage
    model: str
    elapsed_seconds: float


def _extract_json(text: str) -> dict[str, Any]:
    """Parse strict JSON, tolerating fenced code blocks and light chatter."""
    text = (text or "").strip()
    if not text:
        raise ModelError("model returned empty content")
    # Strip markdown fences if present.
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
        raise ModelError("model JSON is not an object")
    except json.JSONDecodeError:
        pass
    # Last resort: first {...} block.
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ModelError("no JSON object found in model output")
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        raise ModelError(f"model JSON parse failed: {exc}") from exc
    if not isinstance(obj, dict):
        raise ModelError("model JSON is not an object")
    return obj


class ModelClient:
    def __init__(
        self,
        config: Config,
        events: EventLog | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.events = events
        self.usage = ModelUsage()
        self._client: httpx.AsyncClient | None = None
        self._transport = transport  # injectable for tests / custom routing

    # ------------------------------------------------------------------ config
    @property
    def is_configured(self) -> bool:
        return bool(self.config.model_api_key)

    @property
    def _base_url(self) -> str:
        return (self.config.model_base_url or DEFAULT_BASE_URL).rstrip("/")

    @property
    def _model(self) -> str:
        return self.config.model_name or DEFAULT_MODEL

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.config.model_api_key}",
            "Content-Type": "application/json",
        }
        # OpenRouter attribution headers (optional, harmless elsewhere).
        title = getattr(self.config, "app_title", None) or "Resurreccion"
        headers.setdefault("X-Title", title)
        return headers

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            kwargs: dict[str, Any] = {
                "timeout": httpx.Timeout(self.config.model_timeout_seconds + 30.0),
            }
            if self._transport is not None:
                kwargs["transport"] = self._transport
            self._client = httpx.AsyncClient(**kwargs)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------- call
    async def call(
        self,
        session_id: str,
        *,
        system: str,
        user: str,
        max_tokens: int | None = None,
        temperature: float | None = None,
        expect_fields: tuple[str, ...] = (),
        max_retries: int = 4,
        screenshot_png: bytes | None = None,
    ) -> ModelReply:
        """One model call returning strict JSON. Retries transient failures.

        When `screenshot_png` is provided (and vision is enabled) it is sent
        as an OpenAI-style image_url content part so vision-capable models
        (GLM-5.3-Flash) can see exactly what the browser sees.
        """
        if not self.is_configured:
            raise ModelConfigError(
                "MODEL_API_KEY is not set — the research agent cannot reason. "
                "Set it in the environment (DeepInfra key recommended)."
            )
        user_content: Any = user
        if screenshot_png and self.config.model_vision_enabled:
            data_url = "data:image/png;base64," + base64.b64encode(screenshot_png).decode("ascii")
            user_content = [
                {"type": "text", "text": user},
                {
                    "type": "image_url",
                    "image_url": {"url": data_url, "detail": "auto"},
                },
            ]
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": max_tokens or self.config.model_max_tokens,
            "temperature": self.config.model_temperature if temperature is None else temperature,
        }
        started = time.monotonic()
        last_error: Exception | None = None
        client = await self._ensure_client()

        for attempt in range(1, max_retries + 1):
            try:
                resp = await client.post(
                    f"{self._base_url}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                )
                if resp.status_code in (429,) or resp.status_code >= 500:
                    last_error = ModelError(
                        f"model endpoint {resp.status_code}: {resp.text[:200]}"
                    )
                    await asyncio.sleep(min(2 ** attempt, 30))
                    continue
                if resp.status_code >= 400:
                    raise ModelError(
                        f"model request rejected ({resp.status_code}): {resp.text[:300]}"
                    )
                data = resp.json()
                choice = (data.get("choices") or [{}])[0]
                message = choice.get("message") or {}
                raw = message.get("content") or ""
                content = _extract_json(raw)
                usage = data.get("usage") or {}
                self.usage.calls += 1
                self.usage.prompt_tokens += int(usage.get("prompt_tokens") or 0)
                self.usage.completion_tokens += int(usage.get("completion_tokens") or 0)
                self.usage.cost_usd += float(usage.get("cost") or 0.0)
                elapsed = time.monotonic() - started
                if expect_fields:
                    missing = [f for f in expect_fields if f not in content]
                    if missing:
                        # Contract violation: retrying the identical prompt
                        # usually reproduces the same defect; surface it now
                        # (the runner records it as a recoverable error).
                        raise ModelContractError(
                            f"model output missing fields: {missing}",
                            missing=missing,
                            raw=raw,
                        )
                if self.events is not None:
                    self.events.append(
                        session_id,
                        level="debug",
                        actor="model",
                        action="model_call",
                        detail={
                            "model": self._model,
                            "attempt": attempt,
                            "elapsed_seconds": round(elapsed, 1),
                            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                        },
                    )
                return ModelReply(
                    content=content,
                    raw=raw,
                    usage=self.usage,
                    model=self._model,
                    elapsed_seconds=elapsed,
                )
            except ModelContractError:
                raise  # contract violations are deterministic — do not retry
            except (httpx.HTTPError, json.JSONDecodeError) as exc:
                last_error = ModelError(f"model transport error: {exc}")
                await asyncio.sleep(min(2 ** attempt, 30))

        self.usage.failed_calls += 1
        raise last_error or ModelError("model call failed")

    # ------------------------------------------------------------ diagnostics
    def usage_dict(self) -> dict[str, Any]:
        return {"model": self._model, "configured": self.is_configured, **self.usage.to_dict()}
