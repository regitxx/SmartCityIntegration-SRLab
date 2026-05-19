"""Unit tests for the walking + journey planners (post-v0.4.13).

Taxi was removed in v0.4.12. Geocoder rewritten in v0.4.13 from a
hardcoded landmark dict to a generic 3-tier chain:

  1. Exact MTR station match (cheap, deterministic, multilingual)
  2. OSM Nominatim with HK viewbox (primary free-text geocoder)
  3. ALS (Lands Department address service, fallback)

These tests verify the tier order and the boundary behaviours
(collision guard, schema rejection of taxi, gpt-oss arg-unwrap) without
hardcoding any specific landmark string → coords assertion.
"""
# ruff: noqa: RUF003  # `×` and `→` in comments are intentional for readability.

from __future__ import annotations

import httpx
import pytest
import respx

from smcity.tools import build_default_registry
from smcity.tools.geo import ALS_URL
from smcity.tools.registry import ToolContext
from smcity.tools.transport_simple_modes import (
    NOMINATIM_URL,
    _exact_mtr_station_match,
    _geocode_one,
)

# --- exact MTR station match ---------------------------------------------


def test_exact_mtr_station_match_english() -> None:
    hit = _exact_mtr_station_match("Kowloon Tong")
    assert hit is not None
    lat, lng = hit
    assert 22.33 < lat < 22.34
    assert 114.17 < lng < 114.18
    assert _exact_mtr_station_match("kowloon tong") == hit
    assert _exact_mtr_station_match("KOWLOON TONG") == hit


def test_exact_mtr_station_match_traditional_chinese() -> None:
    hit_en = _exact_mtr_station_match("Tsim Sha Tsui")
    hit_zh = _exact_mtr_station_match("尖沙咀")
    assert hit_en is not None and hit_zh is not None
    assert hit_en == hit_zh


def test_exact_mtr_station_match_no_substring_false_positive() -> None:
    """v0.4.11 PolyU/CityU bug regression guard: substring-y queries must
    NOT match an MTR station via fuzzy means.
    """
    assert _exact_mtr_station_match("Polytechnic University Hong Kong") is None
    assert _exact_mtr_station_match("City University of Hong Kong") is None
    assert _exact_mtr_station_match("HKU SPACE") is None


def test_exact_mtr_station_match_unknown_returns_none() -> None:
    assert _exact_mtr_station_match("") is None
    assert _exact_mtr_station_match("totally not a station") is None


# --- geocoder tier ordering ----------------------------------------------


@pytest.mark.asyncio
async def test_geocode_one_prefers_exact_mtr_over_nominatim_and_als() -> None:
    """Exact MTR station match wins — neither Nominatim nor ALS should fire."""
    with (
        respx.mock(base_url="https://nominatim.openstreetmap.org", assert_all_called=False) as n,
        respx.mock(base_url=ALS_URL.rsplit("/", 1)[0], assert_all_called=False) as a,
    ):
        nom_route = n.get("/search").mock(return_value=httpx.Response(500))
        als_route = a.get("/lookup").mock(return_value=httpx.Response(500))
        coords = await _geocode_one("Kowloon Tong")
    assert coords is not None
    assert not nom_route.called, "Nominatim must NOT be called when exact MTR match exists"
    assert not als_route.called, "ALS must NOT be called when exact MTR match exists"


@pytest.mark.asyncio
async def test_geocode_one_falls_through_to_nominatim() -> None:
    """Free-text place name → Nominatim (Tier 2).

    OSM-shaped response with importance scores; geocoder picks the highest.
    """
    nominatim_sample = [
        {
            "lat": "22.3290573",
            "lon": "114.1594910",
            "class": "amenity",
            "type": "veterinary",
            "importance": 0.208,
            "display_name": "CityU Veterinary Medical Centre",
        },
        {
            "lat": "22.3400205",
            "lon": "114.1697162",
            "class": "amenity",
            "type": "university",
            "importance": 0.519,
            "display_name": "City University of Hong Kong",
        },
    ]
    with (
        respx.mock(base_url="https://nominatim.openstreetmap.org") as n,
        respx.mock(base_url=ALS_URL.rsplit("/", 1)[0], assert_all_called=False) as a,
    ):
        nom_route = n.get("/search").mock(
            return_value=httpx.Response(200, json=nominatim_sample)
        )
        als_route = a.get("/lookup").mock(return_value=httpx.Response(500))
        coords = await _geocode_one("City University of Hong Kong")
    assert nom_route.called, "Nominatim must be called for non-station free-text"
    assert not als_route.called, "ALS must NOT be called when Nominatim resolved"
    assert coords is not None
    # Picked the higher-importance candidate, not the first one.
    assert abs(coords[0] - 22.3400205) < 1e-5
    assert abs(coords[1] - 114.1697162) < 1e-5


@pytest.mark.asyncio
async def test_geocode_one_falls_through_to_als_when_nominatim_empty() -> None:
    """If Nominatim returns nothing, fall through to ALS for street addresses."""
    als_sample = {
        "SuggestedAddress": [
            {
                "Address": {
                    "PremisesAddress": {
                        "EngPremisesAddress": {"BuildingName": "TEST"},
                        "GeospatialInformation": {
                            "Latitude": "22.3000",
                            "Longitude": "114.1800",
                        },
                    }
                }
            }
        ]
    }
    with (
        respx.mock(base_url="https://nominatim.openstreetmap.org") as n,
        respx.mock(base_url=ALS_URL.rsplit("/", 1)[0]) as a,
    ):
        nom_route = n.get("/search").mock(return_value=httpx.Response(200, json=[]))
        als_route = a.get("/lookup").mock(return_value=httpx.Response(200, json=als_sample))
        coords = await _geocode_one("11 Yuk Choi Road, Hung Hom")
    assert nom_route.called
    assert als_route.called
    assert coords is not None
    assert abs(coords[0] - 22.3000) < 1e-3
    assert abs(coords[1] - 114.1800) < 1e-3


@pytest.mark.asyncio
async def test_geocode_one_returns_none_when_all_tiers_fail() -> None:
    """All three tiers refusing → None (handler will raise a user-visible error)."""
    with (
        respx.mock(base_url="https://nominatim.openstreetmap.org") as n,
        respx.mock(base_url=ALS_URL.rsplit("/", 1)[0]) as a,
    ):
        n.get("/search").mock(return_value=httpx.Response(200, json=[]))
        a.get("/lookup").mock(return_value=httpx.Response(200, json={"SuggestedAddress": []}))
        coords = await _geocode_one("xyz-nonsense-place-name-1234567890")
    assert coords is None


@pytest.mark.asyncio
async def test_geocode_one_nominatim_failure_falls_through_to_als() -> None:
    """If Nominatim errors (timeout, 500, network), ALS should still be tried."""
    als_sample = {
        "SuggestedAddress": [
            {
                "Address": {
                    "PremisesAddress": {
                        "GeospatialInformation": {
                            "Latitude": "22.3100",
                            "Longitude": "114.1700",
                        }
                    }
                }
            }
        ]
    }
    with (
        respx.mock(base_url="https://nominatim.openstreetmap.org") as n,
        respx.mock(base_url=ALS_URL.rsplit("/", 1)[0]) as a,
    ):
        n.get("/search").mock(return_value=httpx.Response(500))
        a.get("/lookup").mock(return_value=httpx.Response(200, json=als_sample))
        coords = await _geocode_one("some place")
    assert coords is not None
    assert abs(coords[0] - 22.3100) < 1e-3


def test_nominatim_url_constant_points_to_real_endpoint() -> None:
    """Avoid silent endpoint drift — pin the URL constant."""
    assert NOMINATIM_URL == "https://nominatim.openstreetmap.org/search"


# --- plan_walking_route --------------------------------------------------


@pytest.mark.asyncio
async def test_plan_walking_route_with_lat_lng() -> None:
    registry = build_default_registry()
    ctx = ToolContext(session_id="w-1")
    # Mong Kok MTR → Holy Cross-ish. About 450 m.
    result = await registry.dispatch(
        "transport.plan_walking_route",
        {
            "origin_lat": 22.3195,
            "origin_lng": 114.1692,
            "destination_lat": 22.3225,
            "destination_lng": 114.1720,
        },
        ctx,
    )
    assert result.status == "ok", result.error
    assert result.result is not None
    assert 200 <= result.result["distance_m"] <= 800
    assert 1 <= result.result["duration_min"] <= 15


# --- plan_journey --------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_journey_returns_walk_and_mtr_only() -> None:
    """No taxi mode in v0.4.12+ — only walk + MTR."""
    registry = build_default_registry()
    ctx = ToolContext(session_id="j-1")
    result = await registry.dispatch(
        "transport.plan_journey",
        {
            "origin_lat": 22.3195,
            "origin_lng": 114.1692,  # Mong Kok
            "destination_lat": 22.3817,
            "destination_lng": 114.1870,  # Sha Tin
        },
        ctx,
    )
    assert result.status == "ok", result.error
    assert result.result is not None
    modes = {opt["mode"] for opt in result.result["options"]}
    assert modes == {"walk", "mtr"}, f"got {modes}; taxi must NOT be present"
    assert result.result["recommendation"] in {"walk", "mtr"}


@pytest.mark.asyncio
async def test_plan_journey_taxi_mode_rejected_by_schema() -> None:
    """Asking for taxi mode is a schema validation error, not a silent fall-through."""
    registry = build_default_registry()
    ctx = ToolContext(session_id="j-taxi")
    result = await registry.dispatch(
        "transport.plan_journey",
        {
            "origin_lat": 22.3195,
            "origin_lng": 114.1692,
            "destination_lat": 22.3817,
            "destination_lng": 114.1870,
            "modes": ["taxi"],
        },
        ctx,
    )
    assert result.status != "ok"


@pytest.mark.asyncio
async def test_plan_journey_unwraps_gpt_oss_nested_args() -> None:
    """gpt-oss-120b sometimes emits `{"name": "...", "arguments": {...}}`
    instead of the unwrapped body. The dispatcher must transparently unwrap.
    """
    registry = build_default_registry()
    ctx = ToolContext(session_id="j-nested")
    result = await registry.dispatch(
        "transport.plan_journey",
        {
            "name": "transport_plan_journey",
            "arguments": {
                "origin_lat": 22.3195,
                "origin_lng": 114.1692,
                "destination_lat": 22.3817,
                "destination_lng": 114.1870,
            },
        },
        ctx,
    )
    assert result.status == "ok", result.error


@pytest.mark.asyncio
async def test_plan_journey_collision_guard_triggers() -> None:
    """Origin and destination resolving to the exact same MTR station must error."""
    registry = build_default_registry()
    ctx = ToolContext(session_id="j-collision")
    result = await registry.dispatch(
        "transport.plan_journey",
        {"origin": "Kowloon Tong", "destination": "Kowloon Tong"},
        ctx,
    )
    assert result.status != "ok"
    assert "resolved to nearly the same" in (result.error or "")


# --- meta.forget_me ------------------------------------------------------


@pytest.mark.asyncio
async def test_meta_forget_me_clears_session(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from smcity.session import SessionStore
    from smcity.slots import LocationSlot, SessionSlots
    from smcity.tools import meta as meta_mod

    db = tmp_path / "s.sqlite3"
    meta_mod._DEFAULT_DB = db
    store = SessionStore(db)
    slots = SessionSlots(session_id="f-1", origin=LocationSlot(raw="Sheung Wan"))
    await store.save(slots)
    loaded = await store.load("f-1")
    assert loaded.origin is not None

    registry = build_default_registry()
    ctx = ToolContext(session_id="f-1")
    result = await registry.dispatch("meta.forget_me", {}, ctx)
    assert result.status == "ok"
    assert result.result is not None
    assert result.result["ok"] is True
    reloaded = await store.load("f-1")
    assert reloaded.origin is None
