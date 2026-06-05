"""Tests for the local POI mirror (smcity.data.poi_store) + find_poi rewire.

Covers four contracts:
  1. Store round-trip — R*Tree bbox filtering, category isolation, limit, the
     atomic per-category swap, and the freshness/`is_populated` semantics.
  2. Parse parity — what the mirror returns equals what a live Overpass query
     would have returned, because both go through `_parse_overpass_elements`.
  3. find_poi routing — local-first hit (no network), fallback-to-live on a
     cold category, and the A/B "fallback disabled" isolation mode.
  4. /health surfaces mirror freshness.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx

from smcity.data.poi_store import PoiStore, get_poi_store
from smcity.settings import get_settings
from smcity.tools.osm_pois import (
    OVERPASS_URL,
    SOURCE_LIVE,
    SOURCE_MIRROR,
    FindPoiArgs,
    OsmPoi,
    _find_poi_handler,
    _parse_overpass_elements,
)
from smcity.tools.registry import ToolContext

# A two-element Overpass payload reused across tests — one node, one way.
_SAMPLE = {
    "elements": [
        {
            "type": "node",
            "id": 1,
            "lat": 22.3096,
            "lon": 114.1657,
            "tags": {
                "name": "7-Eleven",
                "name:en": "7-Eleven",
                "name:zh": "7-11便利店",
                "shop": "convenience",
                "brand": "7-Eleven",
                "opening_hours": "24/7",
            },
        },
        {
            "type": "way",
            "id": 2,
            "center": {"lat": 22.3083, "lon": 114.1699},
            "tags": {"shop": "convenience", "name": "Circle K"},
        },
    ]
}

_HK_BBOX = (22.15, 113.83, 22.58, 114.44)


def _set(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    """Override settings env and clear the memoised getters."""
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    get_poi_store.cache_clear()


# --- 1. store round-trip --------------------------------------------------


def test_store_roundtrip_and_bbox_filter(tmp_path) -> None:
    store = PoiStore(tmp_path / "poi.sqlite")
    pois = _parse_overpass_elements(_SAMPLE)
    written = store.replace_category(
        "convenience_store", [p.model_dump() for p in pois], _now()
    )
    assert written == 2

    # Inside HK bbox -> both returned.
    hits = store.query("convenience_store", _HK_BBOX, limit=20)
    assert {h["name"] for h in hits} == {"7-Eleven", "Circle K"}

    # A bbox far from HK -> R*Tree prunes everything.
    empty = store.query("convenience_store", (0.0, 0.0, 1.0, 1.0), limit=20)
    assert empty == []


def test_store_isolates_categories_and_respects_limit(tmp_path) -> None:
    store = PoiStore(tmp_path / "poi.sqlite")
    pois = _parse_overpass_elements(_SAMPLE)
    store.replace_category("convenience_store", [p.model_dump() for p in pois], _now())
    store.replace_category("supermarket", [pois[0].model_dump()], _now())

    # Querying one category never bleeds rows from another.
    assert len(store.query("supermarket", _HK_BBOX, limit=20)) == 1
    # Limit is honoured.
    assert len(store.query("convenience_store", _HK_BBOX, limit=1)) == 1


def test_replace_category_is_a_full_swap(tmp_path) -> None:
    store = PoiStore(tmp_path / "poi.sqlite")
    pois = _parse_overpass_elements(_SAMPLE)
    store.replace_category("convenience_store", [p.model_dump() for p in pois], _now())
    # Refresh with a single row -> the old two are gone, not merged.
    store.replace_category("convenience_store", [pois[0].model_dump()], _now())
    hits = store.query("convenience_store", _HK_BBOX, limit=20)
    assert [h["name"] for h in hits] == ["7-Eleven"]


def test_is_populated_true_even_for_zero_rows(tmp_path) -> None:
    # A category genuinely empty in HK must count as refreshed, so find_poi does
    # NOT fall back to live Overpass for it forever.
    store = PoiStore(tmp_path / "poi.sqlite")
    assert store.is_populated("drinking_water") is False
    store.replace_category("drinking_water", [], _now())
    assert store.is_populated("drinking_water") is True
    assert store.query("drinking_water", _HK_BBOX, limit=20) == []


def test_freshness_aggregates_oldest_and_total(tmp_path) -> None:
    store = PoiStore(tmp_path / "poi.sqlite")
    store.replace_category("convenience_store", [], "2026-06-01T00:00:00+00:00")
    store.replace_category(
        "supermarket",
        [p.model_dump() for p in _parse_overpass_elements(_SAMPLE)],
        "2026-06-04T00:00:00+00:00",
    )
    fresh = store.freshness()
    assert fresh.categories_populated == 2
    assert fresh.total_pois == 2
    assert fresh.oldest_refresh == "2026-06-01T00:00:00+00:00"
    assert fresh.newest_refresh == "2026-06-04T00:00:00+00:00"


# --- 2. parse parity (mirror == live) -------------------------------------


def test_mirror_returns_same_pois_as_live_parse(tmp_path) -> None:
    """The mirror must reconstruct exactly what a live find_poi would return.

    Both paths flow through `_parse_overpass_elements`; storing and re-reading
    must be lossless for every OsmPoi field we keep. This locks the no-drift
    invariant for the data shape (the registry already locks the tag shape).
    """
    live = _parse_overpass_elements(_SAMPLE)
    store = PoiStore(tmp_path / "poi.sqlite")
    store.replace_category("convenience_store", [p.model_dump() for p in live], _now())

    mirrored = [OsmPoi(**row) for row in store.query("convenience_store", _HK_BBOX, 20)]
    by_id = {p.osm_id: p for p in mirrored}
    assert {p.osm_id for p in live} == set(by_id)
    for original in live:
        assert by_id[original.osm_id] == original  # full pydantic equality


# --- 3. find_poi routing --------------------------------------------------


@pytest.mark.asyncio
async def test_find_poi_local_first_does_not_touch_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set(monkeypatch, POI_STORE_ENABLED="true", POI_OVERPASS_FALLBACK="true")
    store = get_poi_store()
    store.replace_category(
        "convenience_store",
        [p.model_dump() for p in _parse_overpass_elements(_SAMPLE)],
        _now(),
    )

    async def _boom(_query: str) -> dict:
        raise AssertionError("find_poi hit live Overpass despite a populated mirror")

    monkeypatch.setattr("smcity.tools.osm_pois.fetch_overpass", _boom)

    result = await _find_poi_handler(
        FindPoiArgs(category="convenience_store", lat=22.31, lng=114.17, radius_m=5000),
        ToolContext(session_id="t-1"),
    )
    assert result.source == SOURCE_MIRROR
    assert {p.name for p in result.pois} == {"7-Eleven", "Circle K"}


@pytest.mark.asyncio
@respx.mock
async def test_find_poi_falls_back_to_live_when_cold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set(monkeypatch, POI_STORE_ENABLED="true", POI_OVERPASS_FALLBACK="true")
    # Store is empty -> category not populated -> live Overpass.
    respx.post(OVERPASS_URL).mock(return_value=httpx.Response(200, json=_SAMPLE))

    result = await _find_poi_handler(
        FindPoiArgs(category="convenience_store", lat=22.31, lng=114.17, radius_m=5000),
        ToolContext(session_id="t-2"),
    )
    assert result.source == SOURCE_LIVE
    assert len(result.pois) == 2


@pytest.mark.asyncio
async def test_find_poi_fallback_disabled_returns_empty_not_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A/B isolation: mirror cold + fallback off -> honest empty, no live call.
    _set(monkeypatch, POI_STORE_ENABLED="true", POI_OVERPASS_FALLBACK="false")

    async def _boom(_query: str) -> dict:
        raise AssertionError("fallback disabled but find_poi called live Overpass")

    monkeypatch.setattr("smcity.tools.osm_pois.fetch_overpass", _boom)

    result = await _find_poi_handler(
        FindPoiArgs(category="convenience_store", lat=22.31, lng=114.17, radius_m=5000),
        ToolContext(session_id="t-3"),
    )
    assert result.source == SOURCE_MIRROR
    assert result.pois == []


# --- 4. /health surfaces freshness ----------------------------------------


@pytest.mark.asyncio
async def test_health_reports_poi_mirror(monkeypatch: pytest.MonkeyPatch) -> None:
    from smcity.app import _poi_mirror_health

    _set(monkeypatch, POI_REFRESH_INTERVAL_HOURS="24")
    store = get_poi_store()
    store.replace_category(
        "convenience_store",
        [p.model_dump() for p in _parse_overpass_elements(_SAMPLE)],
        _now(),
    )
    block = _poi_mirror_health(get_settings())
    assert block.categories_total == 30
    assert block.categories_populated == 1
    assert block.total_pois == 2
    assert block.stale is False  # just refreshed


def test_health_marks_stale_when_old(monkeypatch: pytest.MonkeyPatch) -> None:
    from smcity.app import _poi_mirror_health

    _set(monkeypatch, POI_REFRESH_INTERVAL_HOURS="24")
    store = get_poi_store()
    store.replace_category(
        "convenience_store",
        [p.model_dump() for p in _parse_overpass_elements(_SAMPLE)],
        "2026-01-01T00:00:00+00:00",  # months old -> > 2x interval
    )
    block = _poi_mirror_health(get_settings())
    assert block.stale is True


# --- 5. refresh module (sweep + cross-replica lock) -----------------------


@pytest.mark.asyncio
async def test_refresh_all_populates_every_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """refresh_all should refresh all 30 categories from Overpass into the store.

    Mock fetch_overpass so every category returns the same sample; throttle off
    so the sweep is instant. This also proves the end-to-end no-drift path:
    refresh -> store -> a later find_poi serves the SAME pois from the mirror.
    """
    from smcity.data import poi_refresh

    async def _fake_fetch(_query: str) -> dict:
        return _SAMPLE

    monkeypatch.setattr(poi_refresh, "fetch_overpass", _fake_fetch)
    store = get_poi_store()
    results = await poi_refresh.refresh_all(store, throttle_s=0.0)

    assert len(results) == 30
    assert all(count == 2 for count in results.values())
    fresh = store.freshness()
    assert fresh.categories_populated == 30
    assert fresh.total_pois == 60


@pytest.mark.asyncio
async def test_refresh_skips_when_another_replica_holds_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cross-replica file-lock must make a second refresher a no-op."""
    from smcity.data import poi_refresh

    store = get_poi_store()
    held = poi_refresh._acquire_lock(poi_refresh._lock_path(store.path))
    assert held is not None  # we are the "other replica"
    try:
        result = await poi_refresh.refresh_once_locked(store)
        assert result is None  # blocked — did not refresh
    finally:
        poi_refresh._release_lock(held)


def _now() -> str:
    return datetime.now(UTC).isoformat()
