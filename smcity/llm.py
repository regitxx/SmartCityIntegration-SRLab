"""Thin wrapper around LM Studio's OpenAI-compatible API.

Supports:
- `ping()`       — reachability + model list.
- `chat()`       — one-shot chat (optionally with tools).
- `chat_stream()` — async generator of `StreamEvent` for incremental UX.

All calls accept `session_id`, which is forwarded as the OpenAI `user` field —
llama.cpp / LM Studio use it to keep a conversation pinned to a single slot
so the KV cache stays warm across turns.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
from openai import AsyncOpenAI, OpenAIError

from smcity.settings import get_settings


@dataclass(slots=True)
class LLMReply:
    text: str
    tool_calls: list[dict[str, Any]]
    usage: dict[str, int]
    elapsed_ms: int


@dataclass(slots=True)
class StreamEvent:
    kind: Literal["token", "tool_call_delta", "final"]
    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    elapsed_ms: int = 0


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


def _build_kwargs(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None,
    temperature: float,
    parallel_tool_calls: bool,
    session_id: str | None,
) -> dict[str, Any]:
    s = get_settings()
    kwargs: dict[str, Any] = {
        "model": s.llm_model,
        "messages": messages,
        "temperature": temperature,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
        kwargs["parallel_tool_calls"] = parallel_tool_calls
    if session_id:
        # llama.cpp + LM Studio route `user` to a per-slot KV cache.
        kwargs["user"] = session_id
    return kwargs


async def chat(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    temperature: float = 0.0,
    parallel_tool_calls: bool = True,
    session_id: str | None = None,
) -> LLMReply:
    """One-shot chat against LM Studio."""
    started = time.perf_counter()
    kwargs = _build_kwargs(
        messages,
        tools=tools,
        temperature=temperature,
        parallel_tool_calls=parallel_tool_calls,
        session_id=session_id,
    )

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


async def chat_stream(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    temperature: float = 0.0,
    parallel_tool_calls: bool = True,
    session_id: str | None = None,
) -> AsyncIterator[StreamEvent]:
    """Stream tokens + tool-call deltas from LM Studio.

    Yields `StreamEvent(kind="token", text=...)` as text deltas arrive and ends
    with `StreamEvent(kind="final", text=<full>, tool_calls=[...], usage=...)`.
    """
    started = time.perf_counter()
    kwargs = _build_kwargs(
        messages,
        tools=tools,
        temperature=temperature,
        parallel_tool_calls=parallel_tool_calls,
        session_id=session_id,
    )
    kwargs["stream"] = True
    kwargs["stream_options"] = {"include_usage": True}

    full_text: list[str] = []
    # Accumulator for tool calls across chunks: index -> {id, name, arguments}
    tool_accum: dict[int, dict[str, Any]] = {}
    usage: dict[str, int] = {}

    try:
        stream = await _client().chat.completions.create(**kwargs)
        async for chunk in stream:
            if chunk.usage is not None:
                usage = chunk.usage.model_dump()
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            content = getattr(delta, "content", None)
            if content:
                full_text.append(content)
                yield StreamEvent(kind="token", text=content)
            tc_deltas = getattr(delta, "tool_calls", None)
            if tc_deltas:
                for tc in tc_deltas:
                    slot = tool_accum.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    fn = getattr(tc, "function", None)
                    if fn is not None:
                        if getattr(fn, "name", None):
                            slot["name"] = fn.name
                        if getattr(fn, "arguments", None):
                            slot["arguments"] += fn.arguments
    except OpenAIError as err:
        raise LLMError(f"LM Studio stream failed: {err}") from err

    tool_calls = [tool_accum[i] for i in sorted(tool_accum)]
    yield StreamEvent(
        kind="final",
        text="".join(full_text),
        tool_calls=tool_calls,
        usage=usage,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )
