"""Tool base class + registry.

A Tool is a typed async callable with:
- a stable `name` (e.g. `transport.get_mtr_next_trains`)
- an `args_schema` (pydantic BaseModel)
- a `result_schema` (pydantic BaseModel)
- a `handler(args, ctx)` coroutine returning the typed result
- per-tool `ttl_seconds` and `budget_ms`

Validation happens at the dispatcher so handlers always see typed args.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

ArgsT = TypeVar("ArgsT", bound=BaseModel)
ResultT = TypeVar("ResultT", bound=BaseModel)


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


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec[Any, Any]] = {}

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
        # Sort deterministically so the system prompt + tool list are cache-friendly.
        return [self._tools[name].openai_schema() for name in sorted(self._tools)]

    async def dispatch(self, name: str, raw_args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        started = time.perf_counter()
        spec = self.get(name)
        try:
            args = spec.args_schema.model_validate(raw_args)
        except Exception as err:
            return ToolResult(
                name=name,
                args=raw_args,
                status="error",
                latency_ms=int((time.perf_counter() - started) * 1000),
                error=f"argument validation failed: {err}",
            )

        try:
            out = await spec.handler(args, ctx)
        except ToolTimeoutError as err:
            return ToolResult(
                name=name,
                args=raw_args,
                status="timeout",
                latency_ms=int((time.perf_counter() - started) * 1000),
                error=str(err),
            )
        except ToolRateLimitedError as err:
            return ToolResult(
                name=name,
                args=raw_args,
                status="rate_limited",
                latency_ms=int((time.perf_counter() - started) * 1000),
                error=str(err),
            )
        except ToolError as err:
            return ToolResult(
                name=name,
                args=raw_args,
                status="error",
                latency_ms=int((time.perf_counter() - started) * 1000),
                error=str(err),
            )
        except Exception as err:
            return ToolResult(
                name=name,
                args=raw_args,
                status="error",
                latency_ms=int((time.perf_counter() - started) * 1000),
                error=f"unexpected: {err}",
            )

        elapsed = int((time.perf_counter() - started) * 1000)
        return ToolResult(
            name=name,
            args=raw_args,
            status="ok",
            latency_ms=elapsed,
            result=out.model_dump(mode="json"),
        )
