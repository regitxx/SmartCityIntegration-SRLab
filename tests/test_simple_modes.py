"""Unit tests for the walking + journey planners (post-v0.4.12).

Taxi was removed in v0.4.12 (see CHANGELOG / feedback-no-taxi memory).
Geocoder was rewritten to: landmark-override → exact-MTR-match → ALS,
with a 100 m collision guard. These tests pin the new behaviour and
prevent regressing the PolyU = CityU bug.
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
    _exact_mtr_station_match,
    _geocode_one,
    _landmark_lookup,
)

# --- geocoder unit tests -------------------------------------------------


def test_landmark_lookup_disneyland_multilingual() -> None:
    """All language variants of 'Disneyland' resolve to the same Lantau coords."""
    expected = (22.31480, 114.04460)
    for q in [
        "disneyland",
        "Disneyland",
        "DISNEYLAND",
        "Hong Kong Disneyland",
        "hk disneyland",
        "迪士尼",
        "迪士尼樂園",  # Traditional
        "迪士尼乐园",  # Simplified
        "香港迪士尼樂園",
        "香港迪士尼乐园",
    ]:
        assert _landmark_lookup(q) == expected, f"failed for {q!r}"


def test_landmark_lookup_universities_multilingual() -> None:
    """The HK university abbreviations + Chinese names all resolve correctly.

    Verifies the v0.4.12 fix for the live boss-demo failure: ALS returned
    a Fanling-area address for the bare query "PolyU", and CUHK-area
    junk for "城市大學" without the 香港 prefix.
    """
    cases = [
        # PolyU — Hung Hom area
        (["polyu", "PolyU", "理工大學", "理工大学", "香港理工"], 22.30410, 114.17907),
        # CityU — Kowloon Tong
        (["cityu", "CityU", "城市大學", "城市大学", "city university"], 22.33612, 114.17418),
        # HKU — Pok Fu Lam main campus
        (["hku", "HKU", "港大", "香港大學", "university of hong kong"], 22.28131, 114.14016),
        # CUHK — Sha Tin
        (["cuhk", "中大", "中文大學", "chinese university"], 22.41940, 114.20680),
        # HKUST — Clear Water Bay
        (["hkust", "ust", "科大", "香港科技大學"], 22.33670, 114.26730),
        # HKBU — Kowloon Tong
        (["hkbu", "浸大", "baptist university"], 22.33930, 114.18030),
    ]
    for variants, lat, lng in cases:
        for q in variants:
            hit = _landmark_lookup(q)
            assert hit is not None, f"no landmark hit for {q!r}"
            assert abs(hit[0] - lat) < 0.0001 and abs(hit[1] - lng) < 0.0001, (
                f"{q!r}: expected ({lat}, {lng}), got {hit}"
            )


def test_landmark_lookup_unknown_returns_none() -> None:
    assert _landmark_lookup("Times Square") is None
    assert _landmark_lookup("") is None
    assert _landmark_lookup("not a real place") is None


def test_exact_mtr_station_match_english() -> None:
    """Exact case-insensitive match against an English station name."""
    # Kowloon Tong station; coords sourced from MTR_STATION_COORDS.
    hit = _exact_mtr_station_match("Kowloon Tong")
    assert hit is not None
    lat, lng = hit
    assert 22.33 < lat < 22.34
    assert 114.17 < lng < 114.18

    # Case-insensitive.
    assert _exact_mtr_station_match("kowloon tong") == hit
    assert _exact_mtr_station_match("KOWLOON TONG") == hit


def test_exact_mtr_station_match_traditional_chinese() -> None:
    """Traditional Chinese station name resolves to the same coords."""
    hit_en = _exact_mtr_station_match("Tsim Sha Tsui")
    hit_zh = _exact_mtr_station_match("尖沙咀")
    assert hit_en is not None and hit_zh is not None
    assert hit_en == hit_zh


def test_exact_mtr_station_match_no_substring_false_positive() -> None:
    """The v0.4.11 PolyU/CityU bug — 'Polytechnic University Hong Kong' was
    matching MTR 'University' station via `fuzz.WRatio`. The exact-match
    matcher must NOT fire on these substring-y queries.
    """
    assert _exact_mtr_station_match("Polytechnic University Hong Kong") is None
    assert _exact_mtr_station_match("City University of Hong Kong") is None
    assert _exact_mtr_station_match("HKU SPACE") is None  # legitimate non-station


def test_exact_mtr_station_match_unknown_returns_none() -> None:
    assert _exact_mtr_station_match("") is None
    assert _exact_mtr_station_match("totally not a station") is None


@pytest.mark.asyncio
async def test_geocode_one_prefers_landmark_override() -> None:
    """Landmark override (Disneyland) wins even though ALS would return junk."""
    # No ALS mock — if landmark override didn't fire, this would hit the live
    # network. Use respx to be sure.
    with respx.mock(base_url=ALS_URL.rsplit("/", 1)[0], assert_all_called=False) as mock:
        mock.get("/lookup").mock(return_value=httpx.Response(500))
        coords = await _geocode_one("Hong Kong Disneyland")
    assert coords == (22.31480, 114.04460)


@pytest.mark.asyncio
async def test_geocode_one_prefers_exact_mtr_over_als() -> None:
    """Exact MTR station match wins over ALS — ALS shouldn't even be called."""
    with respx.mock(base_url=ALS_URL.rsplit("/", 1)[0], assert_all_called=False) as mock:
        route = mock.get("/lookup").mock(return_value=httpx.Response(500))
        coords = await _geocode_one("Kowloon Tong")
    assert coords is not None
    assert route.called is False, "ALS must NOT be called when exact MTR match exists"


@pytest.mark.asyncio
async def test_geocode_one_falls_through_to_als() -> None:
    """A query that's not in landmark_coords and not an MTR station name must
    hit ALS. 'Festival Walk' is the mall in Kowloon Tong — not an MTR station
    and not in the landmark override table, so it's the ALS tier's domain.
    """
    sample = {
        "RequestAddress": {"AddressLine": ["Festival Walk"]},
        "SuggestedAddress": [
            {
                "Address": {
                    "PremisesAddress": {
                        "EngPremisesAddress": {
                            "BuildingName": "FESTIVAL WALK",
                            "EngStreet": {"StreetName": "TAT CHEE AVENUE"},
                        },
                        "GeospatialInformation": {
                            "Latitude": "22.33705",
                            "Longitude": "114.17487",
                        },
                    }
                }
            }
        ],
    }
    with respx.mock(base_url=ALS_URL.rsplit("/", 1)[0]) as mock:
        route = mock.get("/lookup").mock(return_value=httpx.Response(200, json=sample))
        coords = await _geocode_one("Festival Walk")
    assert route.called, "ALS should be hit for non-landmark, non-station names"
    assert coords is not None
    lat, lng = coords
    assert 22.33 < lat < 22.34
    assert 114.17 < lng < 114.18


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
    """No taxi mode in v0.4.12 — only walk + MTR."""
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
    """Asking for taxi mode is now a schema validation error, not a silent fall-through."""
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
    # Pydantic rejects "taxi" before the handler runs → result is an error status.
    assert result.status != "ok"


@pytest.mark.asyncio
async def test_plan_journey_unwraps_gpt_oss_nested_args() -> None:
    """v0.4.12: gpt-oss-120b sometimes emits `{"name": "...", "arguments": {...}}`
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
async def test_plan_journey_routes_to_disneyland_resort() -> None:
    """End-to-end: 'Disneyland' alias → Disneyland Resort coords → planner
    finds DIS station (newly in MTR_STATION_COORDS) within 1500 m.

    Lan Kwai Fong (Central area) → Disneyland should produce an MTR leg
    via Tung Chung Line + Disneyland Resort Line.
    """
    registry = build_default_registry()
    ctx = ToolContext(session_id="j-disney")
    result = await registry.dispatch(
        "transport.plan_journey",
        {"origin": "Lan Kwai Fong", "destination": "Hong Kong Disneyland"},
        ctx,
    )
    assert result.status == "ok", result.error
    assert result.result is not None
    mtr = next(
        (o for o in result.result["options"] if o["mode"] == "mtr"), None
    )
    assert mtr is not None
    # The planner should find DIS station now that it's in MTR_STATION_COORDS.
    # Either the destination station is named, or the route uses DRL.
    summary = mtr.get("mtr_legs_summary") or ""
    assert summary, f"empty MTR summary, note was: {mtr.get('note')!r}"


@pytest.mark.asyncio
async def test_plan_journey_collision_guard_triggers() -> None:
    """Origin and destination resolving to the exact same MTR station must error."""
    registry = build_default_registry()
    ctx = ToolContext(session_id="j-collision")
    # Same station name on both sides → exact MTR match returns identical coords.
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
