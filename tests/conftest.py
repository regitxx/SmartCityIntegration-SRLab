"""pytest fixtures shared across the suite."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest


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


@pytest.fixture(autouse=True)
def _isolate_poi_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the local POI mirror at a per-test tmp DB.

    Keeps every test off the real `state/poi.sqlite` (no repo litter, no
    cross-test pollution). Existing find_poi tests see an EMPTY mirror, so they
    transparently fall back to live Overpass exactly as before. Caches are
    cleared so the override takes effect and is torn down afterwards.
    """
    from smcity.data.poi_store import get_poi_store
    from smcity.settings import get_settings

    monkeypatch.setenv("POI_STORE_PATH", str(tmp_path) + "/poi.sqlite")
    get_settings.cache_clear()
    get_poi_store.cache_clear()
    yield
    get_settings.cache_clear()
    get_poi_store.cache_clear()
