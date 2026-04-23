"""Unit tests for the per-session token-bucket rate limiter."""

from __future__ import annotations

import asyncio

import pytest

from smcity.ratelimit import RateLimiter


@pytest.mark.asyncio
async def test_limiter_disabled_when_rate_is_zero() -> None:
    limiter = RateLimiter(rate_per_min=0, burst=5)
    assert limiter.enabled is False
    for _ in range(100):
        assert await limiter.acquire("s") == 0.0


@pytest.mark.asyncio
async def test_burst_then_blocked() -> None:
    limiter = RateLimiter(rate_per_min=60, burst=3)  # 1 token / sec, burst 3
    assert await limiter.acquire("s") == 0.0
    assert await limiter.acquire("s") == 0.0
    assert await limiter.acquire("s") == 0.0
    wait = await limiter.acquire("s")
    assert wait > 0.0
    assert wait < 2.0  # should be ~1s away


@pytest.mark.asyncio
async def test_sessions_are_isolated() -> None:
    limiter = RateLimiter(rate_per_min=60, burst=2)
    assert await limiter.acquire("alice") == 0.0
    assert await limiter.acquire("alice") == 0.0
    alice_wait = await limiter.acquire("alice")
    bob_first = await limiter.acquire("bob")
    assert alice_wait > 0
    assert bob_first == 0.0  # Bob's bucket is independent.


@pytest.mark.asyncio
async def test_refill_restores_tokens() -> None:
    limiter = RateLimiter(rate_per_min=6000, burst=1)  # 100 tokens/sec
    assert await limiter.acquire("s") == 0.0
    wait = await limiter.acquire("s")
    assert wait > 0.0
    await asyncio.sleep(0.05)  # plenty of refill
    assert await limiter.acquire("s") == 0.0
