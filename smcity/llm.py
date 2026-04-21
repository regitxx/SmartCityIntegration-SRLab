"""Thin wrapper around LM Studio's OpenAI-compatible API.

Phase 0 scope: connectivity check + one-shot chat with optional tool schema.
Streaming, tool dispatch and KV-cache pinning land in later phases.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx
from openai import AsyncOpenAI, OpenAIError

from smcity.settings import get_settings


@dataclass(slots=True)
class LLMReply:
    text: str
    tool_calls: list[dict[str, Any]]
    usage: dict[str, int]
    elapsed_ms: int


class LLMError(RuntimeError):
    """Raised when the LM Studio endpoint is unreachable or returns an unparseable response."""


def _client() -> AsyncOpenAI:
    s = get_settings()
    # LM Studio accepts any string for the api key field.
    return AsyncOpenAI(
        base_url=s.llm_base_url,
        api_key="lm-studio",
        timeout=httpx.Timeout(s.llm_timeout_s, connect=5.0),
        max_retries=0,
    )


async def ping() -> tuple[bool, list[str]]:
    """Return `(reachable, [model_id, ...])`.  Never raises."""
    s = get_settings()
    try:
        async with httpx.AsyncClient(timeout=5.0) as h:
            r = await h.get(f"{s.llm_base_url}/models")
        if r.status_code != 200:
            return False, []
        data = r.json().get("data", [])
        return True, [m.get("id", "") for m in data]
    except (httpx.HTTPError, ValueError):
        return False, []


async def chat(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    temperature: float = 0.0,
    parallel_tool_calls: bool = True,
) -> LLMReply:
    """One-shot chat against LM Studio. No streaming in Phase 0."""
    s = get_settings()
    started = time.perf_counter()
    kwargs: dict[str, Any] = {
        "model": s.llm_model,
        "messages": messages,
        "temperature": temperature,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
        kwargs["parallel_tool_calls"] = parallel_tool_calls

    try:
        resp = await _client().chat.completions.create(**kwargs)
    except OpenAIError as err:
        raise LLMError(f"LM Studio call failed: {err}") from err

    choice = resp.choices[0]
    tool_calls_raw = getattr(choice.message, "tool_calls", None) or []
    tool_calls: list[dict[str, Any]] = [
        {
            "id": tc.id,
            "name": tc.function.name,
            "arguments": tc.function.arguments,
        }
        for tc in tool_calls_raw
    ]
    usage = resp.usage.model_dump() if resp.usage else {}
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return LLMReply(
        text=choice.message.content or "",
        tool_calls=tool_calls,
        usage=usage,
        elapsed_ms=elapsed_ms,
    )
