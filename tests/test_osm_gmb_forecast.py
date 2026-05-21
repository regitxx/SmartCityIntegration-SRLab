"""Tests for the three new v0.3.0 tools: OSM POIs, GMB ETA, 9-day forecast."""
# ruff: noqa: RUF003  # CJK + typographic marks in comments are intentional.

from __future__ import annotations

import httpx
import pytest
import respx

from smcity.tools import build_default_registry
from smcity.tools.osm_pois import (
    _CATEGORIES,
    OVERPASS_URL,
    _bbox_from_point,
    _build_query,
)
from smcity.tools.registry import ToolContext
from smcity.tools.transport_gmb import GMB_BASE

# --- OSM category coverage / query shape ---------------------------------


def test_all_30_categories_have_tag_specs() -> None:
    # Category Literal must match the keys of _CATEGORIES exactly (30 entries).
    assert len(_CATEGORIES) == 30
    required = {
        "convenience_store",
        "supermarket",
        "public_toilet",
        "place_of_worship",
        "mtr_station_entrance",
        "recycling_location",
        "veterinarian",
        "hardware_store",
        "public_elevator",
        "hairdresser",
        "clothes_shop",
        "electronics_shop",
        "department_store",
        "variety_store",
        "houseware_shop",
        "beauty_shop",
        "optician",
        "shoe_shop",
        "greengrocer",
        "marketplace",
        "bookstore",
        "drinking_water",
        "laundry",
        "government_office",
        "kiosk",
        "dentist",
        "bookmaker",
        "bench",
        "shelter",
        "handrail",
    }
    assert required.issubset(set(_CATEGORIES.keys()))


def test_build_query_contains_bbox_and_tag() -> None:
    bbox = (22.15, 113.83, 22.58, 114.44)
    q = _build_query("convenience_store", bbox)
    assert '["shop"="convenience"]' in q
    assert "22.15,113.83,22.58,114.44" in q
    assert "out center" in q
    assert q.count("node") == 1  # one filter × one node line


def test_build_query_expands_multiple_tag_pairs() -> None:
    # `dentist` has two alternatives (amenity=dentist + healthcare=dentist).
    q = _build_query("dentist", (0.0, 0.0, 1.0, 1.0))
    assert '["amenity"="dentist"]' in q
    assert '["healthcare"="dentist"]' in q


def test_bbox_from_point_expands_roughly_correctly() -> None:
    # 500 m around Central HKO should give a bbox ~ 1 km wide each side.
    bbox = _bbox_from_point(22.302, 114.174, 500)
    assert bbox[0] < 22.302 < bbox[2]
    assert bbox[1] < 114.174 < bbox[3]
    # 500 m ≈ 0.0045° lat
    assert 0.003 < (bbox[2] - bbox[0]) < 0.01


# --- dispatcher + Overpass mocking ---------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_osm_pois_parses_overpass_response() -> None:
    sample = {
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
    respx.post(OVERPASS_URL).mock(return_value=httpx.Response(200, json=sample))

    registry = build_default_registry()
    ctx = ToolContext(session_id="o-1")
    result = await registry.dispatch(
        "geo.find_convenience_store",
        {"lat": 22.3, "lng": 114.17, "radius_m": 500},
        ctx,
    )
    assert result.status == "ok", result.error
    assert result.result is not None
    assert len(result.result["pois"]) == 2
    first = result.result["pois"][0]
    assert first["name"] == "7-Eleven"
    assert first["name_zh"] == "7-11便利店"
    assert first["tags"]["brand"] == "7-Eleven"
    assert first["tags"]["opening_hours"] == "24/7"


@pytest.mark.asyncio
@respx.mock
async def test_osm_pois_dedupes_by_osm_id() -> None:
    sample = {
        "elements": [
            {"type": "node", "id": 1, "lat": 22.3, "lon": 114.17, "tags": {}},
            {"type": "node", "id": 1, "lat": 22.3, "lon": 114.17, "tags": {}},  # dup
            {"type": "node", "id": 2, "lat": 22.3, "lon": 114.17, "tags": {}},
        ]
    }
    respx.post(OVERPASS_URL).mock(return_value=httpx.Response(200, json=sample))

    registry = build_default_registry()
    ctx = ToolContext(session_id="o-2")
    result = await registry.dispatch("geo.find_public_toilet", {}, ctx)
    assert result.status == "ok"
    assert result.result is not None
    assert len(result.result["pois"]) == 2


# --- GMB ETA -------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_gmb_eta_tool_twohop_flow() -> None:
    respx.get(f"{GMB_BASE}/route/HKI/1").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "route_id": 2006408,
                        "region": "HKI",
                        "route_code": "1",
                        "directions": [
                            {
                                "route_seq": 1,
                                "orig_en": "The Peak",
                                "dest_en": "Central",
                                "dest_tc": "中環",
                                "orig_tc": "山頂",
                            }
                        ],
                    }
                ]
            },
        )
    )
    respx.get(f"{GMB_BASE}/eta/route-stop/2006408/20001593").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "route_seq": 1,
                        "eta": [
                            {"timestamp": "2026-04-21T21:05:00+08:00", "remarks_en": "Scheduled"}
                        ],
                    }
                ]
            },
        )
    )

    registry = build_default_registry()
    ctx = ToolContext(session_id="g-1")
    result = await registry.dispatch(
        "transport.get_gmb_eta",
        {"region": "HKI", "route_code": "1", "stop_id": "20001593"},
        ctx,
    )
    assert result.status == "ok", result.error
    assert result.result is not None
    assert result.result["route_id"] == 2006408
    assert result.result["destination_en"] == "Central"
    assert len(result.result["etas"]) == 1


# --- 9-day forecast ------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_9day_forecast_parses_hko_payload() -> None:
    from smcity.tools.context import HKO_BASE

    sample = {
        "generalSituation": "A southerly airstream will bring hot weather.",
        "weatherForecast": [
            {
                "forecastDate": "20260422",
                "week": "Wednesday",
                "forecastMaxtemp": {"value": 32, "unit": "C"},
                "forecastMintemp": {"value": 27, "unit": "C"},
                "forecastMaxrh": {"value": 85, "unit": "percent"},
                "forecastMinrh": {"value": 65, "unit": "percent"},
                "forecastWeather": "Partly cloudy with showers.",
                "forecastWind": "East force 3 to 4.",
                "PSR": "High",
            }
        ],
        "updateTime": "2026-04-21T12:00:00+08:00",
    }
    respx.get(HKO_BASE).mock(return_value=httpx.Response(200, json=sample))

    registry = build_default_registry()
    ctx = ToolContext(session_id="w-1", query_lang="en")
    result = await registry.dispatch("context.get_9day_forecast", {}, ctx)
    assert result.status == "ok", result.error
    assert result.result is not None
    assert "hot weather" in result.result["general_situation"]
    assert len(result.result["days"]) == 1
    day = result.result["days"][0]
    assert day["forecast_maxtemp_c"] == 32
    assert day["forecast_maxrh_pct"] == 85
    assert day["psr"] == "High"
