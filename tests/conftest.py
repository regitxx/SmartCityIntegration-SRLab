"""pytest fixtures shared across the suite."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator

import httpx
import pytest


@pytest.fixture
def event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


def _lm_studio_reachable() -> bool:
    """Cheap pre-check for integration tests.

    Skips instead of fails so `pytest -m "not integration"` stays green off-Tailscale.
    """
    from smcity.settings import get_settings

    url = f"{get_settings().llm_base_url}/models"
    try:
        r = httpx.get(url, timeout=2.0)
        return r.status_code == 200
    except (httpx.HTTPError, OSError):
        return False


@pytest.fixture(scope="session")
def lm_studio_available() -> bool:
    if os.getenv("SKIP_INTEGRATION") == "1":
        return False
    return _lm_studio_reachable()
