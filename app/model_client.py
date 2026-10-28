"""Real LLM client for the research agent.

OpenAI-compatible chat-completions JSON client with native vision support.
Default endpoint is DeepInfra serving **MiMo-V2.6-Flash** (Xiaomi: natively
multimodal, 1M context, reasoning/thinking model): it thinks, orchestrates,
reads the live browser screenshot, and emits JSON. Any OpenAI-compatible endpoint works —
configured entirely via environment variables:

- MODEL_API_KEY         (required for research; absent -> model calls fail
                         with ModelConfigError)
- MODEL_BASE_URL        (default https://api.deepinfra.com/v1/openai)
- MODEL_NAME            (default XiaomiMiMo/MiMo-V2.6-Flash)
- MODEL_MAX_TOKENS      (default 8192)
- MODEL_TEMPERATURE     (default 0.4)
- MODEL_THINKING        (default true — ask a reasoning model to think)
- MODEL_REASONING_EFFORT(default high — none|low|medium|high)
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

from app.config import DEFAULT_BASE_URL, DEFAULT_MODEL, Config
from app.events import EventLog
from app.redact import scrub_url
from app.thoughts import STATUS_DONE, STATUS_FAILED, ThoughtStore

# DEFAULT_BASE_URL / DEFAULT_MODEL are re-exported from app.config (single
# source of truth) — callers and tests import them from here.

# Hard cap on owner reference images sent per call (defense in depth; the
# runner already caps its own list) so a pathological upload set can never
# blow up a request payload.
MAX_REFERENCE_IMAGES_PARTS = 6


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


class _Retryable(Exception):
    """Transient failure of ONE attempt (429/5xx/transport/bad body):
    back off and retry, exactly as the plain-POST path always has."""


# Request-level rejections that plausibly mean "this endpoint will not take
# `stream`": fall back to the plain POST instead of failing the call. Auth
# and billing (401/402/403) are deliberately NOT here — a second identical
# request would fail the same way, so surface it at once.
_STREAM_UNSUPPORTED = frozenset({400, 405, 415, 422, 501})

# Providers spell reasoning differently: DeepInfra documents
# `reasoning_content`, OpenRouter uses `reasoning` (string) or
# `reasoning_details` (list of {text}), and a vLLM-hosted reasoner without a
# reasoning parser leaves the model's ```-think block inside the answer text.
# All three shapes are normalized here so the owner's chain of thought shows
# real reasoning regardless of how the endpoint spells it.
# Built from fragments so the tags never appear as a contiguous literal
# here (same reason vLLM ships its parser as a string constant).
_THINK_TAGS = (
    ("<" + "think>", "</" + "think>"),
    ("<" + "thinking>", "</" + "thinking>"),
)
_THINK_PATTERNS = tuple(
    re.compile(re.escape(op) + r"(.*?)" + re.escape(cl), re.DOTALL)
    for op, cl in _THINK_TAGS
)

# Written into the thought when thinking is ON but the provider exposed no
# reasoning text at all — the owner gets the REASON instead of the bare
# "(no reasoning recorded)" placeholder. It is clearly our text, never
# model output.
_NO_REASONING_NOTE = (
    "(No reasoning text was returned by the provider for this call, so there "
    "is nothing to show — the model answered without exposing "
    "chain-of-thought. Check MODEL_THINKING / MODEL_REASONING_EFFORT and "
    "whether the endpoint documents a reasoning field for this model.)"
)


def _reasoning_text(value: Any) -> str:
    """Normalize any provider spelling of a reasoning field to plain text."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                t = item.get("text") or item.get("content") or item.get("reasoning")
                if isinstance(t, str):
                    parts.append(t)
        return "".join(parts)
    return ""


def _split_think(text: str) -> tuple[str, str]:
    """(think block recovered from the text, text with the block removed).

    The fallback of last resort: the reasoner tagged its thinking but the
    server never split it into a reasoning field. The thinking is the
    owner's chain of thought — it must not be lost, and it must not leak
    into the JSON parse either.
    """
    found: list[str] = []

    def _collect(match: "re.Match[str]") -> str:
        found.append(match.group(1))
        return ""

    for pattern in _THINK_PATTERNS:
        text = pattern.sub(_collect, text)
    return "\n".join(part for part in found if part.strip()), text


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
    # The owner-visible thought this call opened (None when no store is
    # wired, e.g. unit tests). Lets the runner upgrade its summary.
    thought_id: int | None = None


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
        thoughts: ThoughtStore | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.events = events
        # Owner-only live chain of thought. Optional so a bare client still
        # works; when wired, every call opens exactly one thought row.
        self.thoughts = thoughts
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
        reference_images: list[bytes] | None = None,
        label: str = "Thinking",
    ) -> ModelReply:
        """One model call returning strict JSON. Retries transient failures.

        When `screenshot_png` is provided (and vision is enabled) it is sent
        as an OpenAI-style image_url content part so vision-capable models
        (MiMo-V2.6-Flash) can see exactly what the browser sees.
        `reference_images` (already prepared JPEG/PNG bytes) are appended
        after it — the owner's uploaded brief images, so an image-only or
        image-steered session actually reaches the model's eyes.

        When the endpoint supports it the request is streamed, so the
        reasoning arrives token by token and the owner can watch it live
        (one thought row per call, labeled `label`). The stream is an
        enhancement only — see `_request` for the fallback contract: any
        endpoint that will not stream gets exactly the plain request.
        """
        if not self.is_configured:
            raise ModelConfigError(
                "MODEL_API_KEY is not set — the research agent cannot reason. "
                "Set it in the environment (DeepInfra key recommended)."
            )
        user_content: Any = user
        if self.config.model_vision_enabled and (screenshot_png or reference_images):
            parts: list[dict[str, Any]] = [{"type": "text", "text": user}]
            if screenshot_png:
                parts.append({
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64," + base64.b64encode(screenshot_png).decode("ascii"),
                        "detail": "auto",
                    },
                })
            for img in (reference_images or [])[:MAX_REFERENCE_IMAGES_PARTS]:
                parts.append({
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/jpeg;base64," + base64.b64encode(img).decode("ascii"),
                        "detail": "auto",
                    },
                })
            user_content = parts
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
        # Thinking. MiMo-V2.6-Flash is a reasoning model; DeepInfra takes
        # `reasoning_effort` on reasoning-capable models and ignores it on
        # ones that aren't, so this stays safe across model swaps. Omitted
        # entirely when MODEL_THINKING=false.
        if self.config.model_thinking_enabled:
            payload["reasoning_effort"] = self.config.model_reasoning_effort
        started = time.monotonic()
        last_error: Exception | None = None
        client = await self._ensure_client()
        # One owner-visible thought per call. Bookkeeping must never be able
        # to break research: if the store refuses, the call runs without one.
        thought_id = self._begin_thought(session_id, label)

        for attempt in range(1, max_retries + 1):
            try:
                data = await self._request(payload, client, thought_id=thought_id)
                choice = (data.get("choices") or [{}])[0]
                message = choice.get("message") or {}
                # Non-streamed answers carry their reasoning on the message;
                # `_capture_reasoning` skips it when the stream already fed
                # the thought, so the text is never recorded twice.
                self._capture_reasoning(thought_id, message)
                raw = message.get("content") or ""
                # A reasoner whose thinking the server never split out keeps
                # its think block inside the answer text: recover it for the
                # owner's thought and keep it out of the JSON parse.
                think, answer = _split_think(raw)
                if think:
                    if (
                        self.thoughts is not None and thought_id is not None
                        and not self.thoughts.has_detail(thought_id)
                    ):
                        try:
                            self.thoughts.append(thought_id, think)
                        except Exception:
                            pass
                    raw = answer
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
                self._finish_thought(thought_id, status=STATUS_DONE)
                return ModelReply(
                    content=content,
                    raw=raw,
                    usage=self.usage,
                    model=self._model,
                    elapsed_seconds=elapsed,
                    thought_id=thought_id,
                )
            except ModelContractError:
                # Deterministic — never retried — but the model DID think and
                # answer, so the thought closes as done. Its raw text stays
                # owner-only: it never enters events, evidence or reports.
                self._finish_thought(thought_id, status=STATUS_DONE)
                raise
            except _Retryable as exc:
                last_error = ModelError(str(exc))
                await asyncio.sleep(min(2 ** attempt, 30))
                continue
            except (httpx.HTTPError, json.JSONDecodeError) as exc:
                last_error = ModelError(f"model transport error: {exc}")
                await asyncio.sleep(min(2 ** attempt, 30))
            except ModelError:
                # 4xx from the provider: retrying is pointless, but the
                # thought must not stay "streaming" forever.
                self._finish_thought(thought_id, status=STATUS_FAILED)
                raise
            except BaseException:
                self._finish_thought(thought_id, status=STATUS_FAILED)
                raise

        self.usage.failed_calls += 1
        self._finish_thought(thought_id, status=STATUS_FAILED)
        raise last_error or ModelError("model call failed")

    # ------------------------------------------- streaming & thought rows
    def _begin_thought(self, session_id: str, label: str) -> int | None:
        if self.thoughts is None:
            return None
        try:
            return self.thoughts.begin(session_id, label=label)
        except Exception:
            return None

    def _finish_thought(self, thought_id: int | None, *, status: str) -> None:
        """Close the owner's thought row. The observation layer is never
        allowed to fail a model call, hence the swallow (with comment)."""
        if self.thoughts is None or thought_id is None:
            return
        try:
            # Thinking is on but nothing reasoning-shaped ever arrived: say
            # WHY, instead of leaving the owner staring at a placeholder.
            if (
                status == STATUS_DONE
                and self.config.model_thinking_enabled
                and not self.thoughts.has_detail(thought_id)
            ):
                self.thoughts.append(thought_id, _NO_REASONING_NOTE)
            self.thoughts.finish(thought_id, status=status)
        except Exception:
            pass

    def _capture_reasoning(self, thought_id: int | None, message: Any) -> None:
        """Plain (non-streamed) answers carry their reasoning here under any
        of the documented spellings (`reasoning_content`, `reasoning`,
        `reasoning_details`). Captured only when the stream did not already
        write it into the thought."""
        if self.thoughts is None or thought_id is None or not isinstance(message, dict):
            return
        if self.thoughts.has_detail(thought_id):
            return
        text = (
            _reasoning_text(message.get("reasoning_content"))
            or _reasoning_text(message.get("reasoning"))
            or _reasoning_text(message.get("reasoning_details"))
        )
        if text:
            try:
                self.thoughts.append(thought_id, text)
            except Exception:
                pass

    async def _request(
        self,
        payload: dict[str, Any],
        client: httpx.AsyncClient,
        *,
        thought_id: int | None,
    ) -> dict[str, Any]:
        """One HTTP attempt against the chat-completions endpoint.

        Streaming comes first: it is what makes the owner's live chain of
        thought possible. The plain POST below is the CONTRACT — any
        endpoint that will not (or did not) stream gets exactly the
        pre-streaming request, so research never depends on provider
        streaming support. Raises _Retryable (429/5xx/transport) or
        ModelError (4xx, no retry).
        """
        if self.config.model_stream_enabled:
            data = await self._post_stream(client, payload, thought_id=thought_id)
            if data is not None:
                return data
            # The stream produced nothing usable. Whatever reasoning it did
            # emit came from a request we are about to replace — drop it so
            # the thought reflects exactly one answer.
            if self.thoughts is not None and thought_id is not None:
                try:
                    self.thoughts.restart(thought_id)
                except Exception:
                    pass
        url = f"{self._base_url}/chat/completions"
        try:
            resp = await client.post(url, headers=self._headers(), json=payload)
        except httpx.HTTPError as exc:
            raise _Retryable(f"model transport error: {exc}") from exc
        if resp.status_code in (429,) or resp.status_code >= 500:
            raise _Retryable(f"model endpoint {resp.status_code}: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise ModelError(f"model request rejected ({resp.status_code}): {resp.text[:300]}")
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise _Retryable(f"model transport error: {exc}") from exc

    async def _post_stream(
        self,
        client: httpx.AsyncClient,
        payload: dict[str, Any],
        *,
        thought_id: int | None,
    ) -> dict[str, Any] | None:
        """POST with `stream: true`, consuming the SSE body.

        Returns a non-streaming-shaped response dict on success, or None when
        this endpoint did not really stream (rejected the parameter, answered
        with plain JSON, or produced no usable content) so the caller falls
        back to the plain POST. Reasoning deltas are pushed into the owner's
        thought as they arrive — that is the live chain of thought.
        """
        url = f"{self._base_url}/chat/completions"
        body = {**payload, "stream": True}
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        usage: dict[str, Any] = {}
        chunks = 0
        try:
            async with client.stream("POST", url, headers=self._headers(), json=body) as resp:
                if resp.status_code != 200:
                    raw = (await resp.aread()).decode("utf-8", "replace")
                    if resp.status_code in (429,) or resp.status_code >= 500:
                        raise _Retryable(f"model endpoint {resp.status_code}: {raw[:200]}")
                    if resp.status_code in _STREAM_UNSUPPORTED:
                        return None  # "stream not accepted here" → plain POST
                    raise ModelError(f"model request rejected ({resp.status_code}): {raw[:300]}")
                ctype = resp.headers.get("content-type", "")
                if "text/event-stream" not in ctype:
                    # The endpoint ignored `stream` and answered normally —
                    # use that body as-is rather than paying for a second
                    # identical request.
                    raw = (await resp.aread()).decode("utf-8", "replace")
                    if not raw.strip():
                        return None
                    try:
                        return json.loads(raw)
                    except json.JSONDecodeError as exc:
                        raise _Retryable(f"model transport error: {exc}") from exc
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    token = line[5:].strip()
                    if token == "[DONE]":
                        break
                    chunks += 1
                    try:
                        chunk = json.loads(token)
                    except json.JSONDecodeError:
                        continue  # keep-alive / malformed line — never fatal
                    if isinstance(chunk.get("usage"), dict):
                        # DeepInfra puts usage on the final chunk. A provider
                        # that omits it costs this call its token count —
                        # cheaper than a second request to find out.
                        usage = chunk["usage"]
                    for ch in chunk.get("choices") or []:
                        if not isinstance(ch, dict):
                            continue
                        delta = ch.get("delta") or {}
                        if not isinstance(delta, dict):
                            continue
                        for field in ("reasoning_content", "reasoning",
                                      "reasoning_details"):
                            piece = _reasoning_text(delta.get(field))
                            if piece:
                                reasoning_parts.append(piece)
                                if self.thoughts is not None and thought_id is not None:
                                    try:
                                        self.thoughts.append(thought_id, piece)
                                    except Exception:
                                        pass
                        piece = delta.get("content")
                        if isinstance(piece, str) and piece:
                            content_parts.append(piece)
        except httpx.HTTPError as exc:
            raise _Retryable(f"model transport error: {exc}") from exc

        raw_content = "".join(content_parts)
        if chunks == 0 or not raw_content.strip():
            return None  # nothing usable — the plain POST is the contract
        message: dict[str, Any] = {"content": raw_content}
        reasoning = "".join(reasoning_parts)
        if reasoning:
            message["reasoning_content"] = reasoning
        return {"choices": [{"message": message}], "usage": usage}

    # ------------------------------------------------------------ diagnostics
    def usage_dict(self) -> dict[str, Any]:
        return {"model": self._model, "configured": self.is_configured, **self.usage.to_dict()}
