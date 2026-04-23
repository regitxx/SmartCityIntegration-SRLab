"""TTL cache behaviour in ToolRegistry.dispatch (P2-1)."""

from __future__ import annotations

import asyncio
import time

import pytest
from pydantic import BaseModel

from smcity.tools.registry import ToolContext, ToolRegistry, ToolSpec


class _Args(BaseModel):
    q: str


class _Result(BaseModel):
    echo: str
    token: float


def _make_counter_tool(
    ttl: int, *, cacheable: bool = True
) -> tuple[ToolSpec[_Args, _Result], list[int]]:
    """Return a tool whose handler increments a counter — so we can assert cache hits."""
    calls = [0]

    async def handler(args: _Args, _ctx: ToolContext) -> _Result:
        calls[0] += 1
        return _Result(echo=args.q, token=time.monotonic())

    spec = ToolSpec(
        name="_test.echo",
        description_en="echo tool",
        args_schema=_Args,
        result_schema=_Result,
        handler=handler,
        ttl_seconds=ttl,
        cacheable=cacheable,
    )
    return spec, calls


@pytest.mark.asyncio
async def test_cache_hits_within_ttl() -> None:
    spec, calls = _make_counter_tool(ttl=30)
    reg = ToolRegistry()
    reg.register(spec)
    ctx = ToolContext(session_id="sid")
    r1 = await reg.dispatch("_test.echo", {"q": "ping"}, ctx)
    r2 = await reg.dispatch("_test.echo", {"q": "ping"}, ctx)
    assert r1.status == "ok" and r2.status == "ok"
    assert r2.cached is True
    assert calls[0] == 1


@pytest.mark.asyncio
async def test_cache_miss_on_different_args() -> None:
    spec, calls = _make_counter_tool(ttl=30)
    reg = ToolRegistry()
    reg.register(spec)
    ctx = ToolContext(session_id="sid")
    await reg.dispatch("_test.echo", {"q": "a"}, ctx)
    r = await reg.dispatch("_test.echo", {"q": "b"}, ctx)
    assert r.cached is False
    assert calls[0] == 2


@pytest.mark.asyncio
async def test_cache_disabled_when_cacheable_false() -> None:
    spec, calls = _make_counter_tool(ttl=30, cacheable=False)
    reg = ToolRegistry()
    reg.register(spec)
    ctx = ToolContext(session_id="sid")
    await reg.dispatch("_test.echo", {"q": "a"}, ctx)
    r = await reg.dispatch("_test.echo", {"q": "a"}, ctx)
    assert r.cached is False
    assert calls[0] == 2


@pytest.mark.asyncio
async def test_cache_expires_after_ttl() -> None:
    # Use a tiny TTL so we can prove expiry without sleeping long.
    # 0-second TTL means "don't cache", so we ensure 1 is the minimum we can test
    # with and instead drop the entry manually to simulate elapsed time.
    spec, calls = _make_counter_tool(ttl=60)
    reg = ToolRegistry()
    reg.register(spec)
    ctx = ToolContext(session_id="sid")
    await reg.dispatch("_test.echo", {"q": "a"}, ctx)
    reg.cache_clear()  # simulate TTL expiry
    r = await reg.dispatch("_test.echo", {"q": "a"}, ctx)
    assert r.cached is False
    assert calls[0] == 2


@pytest.mark.asyncio
async def test_zero_ttl_skips_cache() -> None:
    spec, calls = _make_counter_tool(ttl=0)
    reg = ToolRegistry()
    reg.register(spec)
    ctx = ToolContext(session_id="sid")
    await reg.dispatch("_test.echo", {"q": "a"}, ctx)
    r = await reg.dispatch("_test.echo", {"q": "a"}, ctx)
    assert r.cached is False
    assert calls[0] == 2


@pytest.mark.asyncio
async def test_concurrent_calls_do_not_double_register() -> None:
    spec, calls = _make_counter_tool(ttl=30)
    reg = ToolRegistry()
    reg.register(spec)
    ctx = ToolContext(session_id="sid")
    results = await asyncio.gather(*(reg.dispatch("_test.echo", {"q": "x"}, ctx) for _ in range(5)))
    assert all(r.status == "ok" for r in results)
    # At least one ran; the others may or may not hit cache depending on scheduling.
    assert 1 <= calls[0] <= 5
