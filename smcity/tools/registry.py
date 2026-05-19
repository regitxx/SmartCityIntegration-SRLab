"""Tool base class + registry.

A Tool is a typed async callable with:
- a stable `name` (e.g. `transport.get_mtr_next_trains`)
- an `args_schema` (pydantic BaseModel)
- a `result_schema` (pydantic BaseModel)
- a `handler(args, ctx)` coroutine returning the typed result
- per-tool `ttl_seconds` and `budget_ms`

Validation happens at the dispatcher so handlers always see typed args. The
dispatcher also honours `ttl_seconds` via an in-memory cache keyed by
`(name, stable-json(args))`; pass `cacheable=False` on the spec to opt out
per-tool.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

import structlog
from pydantic import BaseModel

ArgsT = TypeVar("ArgsT", bound=BaseModel)
ResultT = TypeVar("ResultT", bound=BaseModel)

_log = structlog.get_logger("smcity.tool")


class ToolError(RuntimeError):
    kind: str = "error"


class ToolTimeoutError(ToolError):
    kind = "timeout"


class ToolRateLimitedError(ToolError):
    kind = "rate_limited"


class ToolUpstreamError(ToolError):
    kind = "upstream_error"


class ToolValidationError(ToolError):
    kind = "validation_error"


@dataclass(slots=True)
class ToolContext:
    """Passed to handlers so they can resolve shared resources."""

    session_id: str
    locale: str = "en"
    query_lang: str = "en"  # language of the translated query we'll pass upstream
    translation_applied: bool = False


@dataclass(slots=True)
class ToolSpec(Generic[ArgsT, ResultT]):  # noqa: UP046  # PEP 695 + dataclass has known gotchas
    name: str
    description_en: str
    args_schema: type[ArgsT]
    result_schema: type[ResultT]
    handler: Callable[[ArgsT, ToolContext], Awaitable[ResultT]]
    ttl_seconds: int = 30
    budget_ms: int = 1500
    cacheable: bool = True
    upstream_langs: frozenset[str] = field(default_factory=lambda: frozenset({"en"}))
    upstream: str = ""
    safety_class: str = "read"

    def openai_schema(self) -> dict[str, Any]:
        """Emit the tool schema in the OpenAI function-calling format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description_en,
                "parameters": self.args_schema.model_json_schema(),
            },
        }


@dataclass(slots=True)
class ToolResult:
    name: str
    args: dict[str, Any]
    status: str  # "ok" | "error" | "timeout" | "rate_limited"
    latency_ms: int
    result: dict[str, Any] | None = None
    error: str | None = None
    cached: bool = False


def _stable_args_key(name: str, args: dict[str, Any]) -> str:
    """Deterministic cache key for `(tool, args)`.

    We hash so the key doesn't leak PII (addresses, user-typed strings) into
    debug logs or memory dumps, and so the key stays bounded for large args.
    """
    blob = json.dumps({"n": name, "a": args}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class _CacheEntry:
    expires_at: float
    result: dict[str, Any]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec[Any, Any]] = {}
        self._cache: dict[str, _CacheEntry] = {}

    def register(self, spec: ToolSpec[Any, Any]) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool {spec.name!r} already registered")
        self._tools[spec.name] = spec

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return sorted(self._tools)

    def get(self, name: str) -> ToolSpec[Any, Any]:
        try:
            return self._tools[name]
        except KeyError as err:
            raise ToolValidationError(f"unknown tool: {name!r}") from err

    def openai_schemas(self) -> list[dict[str, Any]]:
        # Deterministic alphabetical order keeps the prompt prefix stable so
        # the LM Studio KV cache survives across turns.
        return [self._tools[name].openai_schema() for name in sorted(self._tools)]

    def _cache_get(self, key: str) -> dict[str, Any] | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        if entry.expires_at < time.monotonic():
            # Lazy eviction — avoids a background sweeper.
            self._cache.pop(key, None)
            return None
        return entry.result

    def _cache_put(self, key: str, result: dict[str, Any], ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            return
        self._cache[key] = _CacheEntry(
            expires_at=time.monotonic() + ttl_seconds,
            result=result,
        )

    def cache_clear(self) -> None:
        self._cache.clear()

    async def dispatch(self, name: str, raw_args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        # Local import — observability module is optional in test contexts.
        from smcity.observability import get_tracer, set_attr_safe

        tracer = get_tracer("smcity.tools")
        with tracer.start_as_current_span(
            f"tool.{name}",
            attributes={
                "tool.name": name,
                "session.id": ctx.session_id,
            },
        ) as span:
            result = await self._dispatch_inner(name, raw_args, ctx, span)
            set_attr_safe(span, "tool.status", result.status)
            set_attr_safe(span, "tool.latency_ms", result.latency_ms)
            set_attr_safe(span, "tool.cached", result.cached)
            if result.error:
                set_attr_safe(span, "tool.error", result.error)
            if isinstance(result.result, dict):
                # Truncated JSON of the result for Phoenix UI inspection. Real
                # payloads can be ~5-50 KB; we cap at 8 KB to keep span ingest
                # cost down.
                import contextlib
                import json as _json

                with contextlib.suppress(Exception):
                    set_attr_safe(
                        span,
                        "tool.result",
                        _json.dumps(result.result, ensure_ascii=False)[:8192],
                    )
            return result

    async def _dispatch_inner(
        self,
        name: str,
        raw_args: dict[str, Any],
        ctx: ToolContext,
        span: Any,  # OTel span
    ) -> ToolResult:
        # Local import to keep registry test-context clean.
        from smcity.observability import set_attr_safe

        started = time.perf_counter()
        spec = self.get(name)
        # gpt-oss-120b sometimes double-wraps tool args in the OpenAI
        # function-call envelope: `{"name": "...", "arguments": {...}}`
        # instead of the unwrapped `{...}` body. Detected live in v0.4.12.
        # Unwrap defensively when the shape is unambiguous.
        if (
            isinstance(raw_args, dict)
            and set(raw_args.keys()) == {"name", "arguments"}
            and isinstance(raw_args.get("arguments"), dict)
        ):
            raw_args = raw_args["arguments"]
        # Record sanitised args for Phoenix — JSON-encoded, capped at 4 KB so
        # we don't fill a span with a giant blob.
        import contextlib
        import json as _json

        with contextlib.suppress(Exception):
            set_attr_safe(
                span,
                "tool.args",
                _json.dumps(raw_args, ensure_ascii=False)[:4096],
            )
        try:
            args = spec.args_schema.model_validate(raw_args)
        except Exception as err:
            elapsed = int((time.perf_counter() - started) * 1000)
            _log.info(
                "tool_call",
                name=name,
                status="error",
                error_kind="validation",
                latency_ms=elapsed,
                session_id=ctx.session_id,
                cached=False,
            )
            return ToolResult(
                name=name,
                args=raw_args,
                status="error",
                latency_ms=elapsed,
                error=f"argument validation failed: {err}",
            )

        cache_key: str | None = None
        if spec.cacheable and spec.ttl_seconds > 0:
            cache_key = _stable_args_key(name, args.model_dump(mode="json"))
            cached = self._cache_get(cache_key)
            if cached is not None:
                elapsed = int((time.perf_counter() - started) * 1000)
                _log.info(
                    "tool_call",
                    name=name,
                    status="ok",
                    latency_ms=elapsed,
                    session_id=ctx.session_id,
                    cached=True,
                )
                return ToolResult(
                    name=name,
                    args=raw_args,
                    status="ok",
                    latency_ms=elapsed,
                    result=cached,
                    cached=True,
                )

        try:
            out = await spec.handler(args, ctx)
        except ToolTimeoutError as err:
            return _fail(name, raw_args, started, "timeout", err, ctx)
        except ToolRateLimitedError as err:
            return _fail(name, raw_args, started, "rate_limited", err, ctx)
        except ToolError as err:
            return _fail(name, raw_args, started, "error", err, ctx)
        except Exception as err:  # pragma: no cover — belt and braces
            return _fail(name, raw_args, started, "error", err, ctx, wrap=True)

        elapsed = int((time.perf_counter() - started) * 1000)
        payload = out.model_dump(mode="json")
        if cache_key is not None:
            self._cache_put(cache_key, payload, spec.ttl_seconds)
        _log.info(
            "tool_call",
            name=name,
            status="ok",
            latency_ms=elapsed,
            session_id=ctx.session_id,
            cached=False,
        )
        return ToolResult(
            name=name,
            args=raw_args,
            status="ok",
            latency_ms=elapsed,
            result=payload,
        )


def _fail(
    name: str,
    raw_args: dict[str, Any],
    started: float,
    status_: str,
    err: Exception,
    ctx: ToolContext,
    *,
    wrap: bool = False,
) -> ToolResult:
    elapsed = int((time.perf_counter() - started) * 1000)
    message = f"unexpected: {err}" if wrap else str(err)
    _log.info(
        "tool_call",
        name=name,
        status=status_,
        latency_ms=elapsed,
        session_id=ctx.session_id,
        cached=False,
        error=message[:200],
    )
    return ToolResult(
        name=name,
        args=raw_args,
        status=status_,
        latency_ms=elapsed,
        error=message,
    )
