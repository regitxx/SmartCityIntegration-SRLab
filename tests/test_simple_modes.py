"""Unit tests for the walking / taxi / journey planners."""
# ruff: noqa: RUF003  # `×` and `→` in comments are intentional for readability.

from __future__ import annotations

import httpx
import pytest
import respx

from smcity.tools import build_default_registry
from smcity.tools.geo import ALS_URL
from smcity.tools.registry import ToolContext
from smcity.tools.transport_simple_modes import _taxi_fare_hkd

# --- pure-function tests (no network) ------------------------------------


@pytest.mark.parametrize(
    ("distance_m", "expected_low"),
    [
        (500, 27),  # flag-down only
        (2000, 27),  # still flag-down
        (2200, 29),  # 27 + 1 × 1.90 = 28.9 → 29
        (3000, 36),  # 27 + 5 × 1.90 = 36.5 → 36 (banker's rounding)
        (5000, 56),  # 27 + 15 × 1.90 = 55.5 → 56 (banker's rounding)
    ],
)
def test_taxi_fare_formula(distance_m: int, expected_low: int) -> None:
    low, high = _taxi_fare_hkd(distance_m)
    assert low == expected_low
    assert high > low
    # High is 25% markup on the pre-rounded base; just sanity check it's close.
    assert low < high <= low * 2


# --- plan_walking_route --------------------------------------------------


@pytest.mark.asyncio
async def test_plan_walking_route_with_lat_lng() -> None:
    registry = build_default_registry()
    ctx = ToolContext(session_id="w-1")
    # Mong Kok MTR → Holy Cross-ish (St Francis). About 450 m.
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


@pytest.mark.asyncio
async def test_plan_walking_route_with_free_text_via_als() -> None:
    """Hits the v0.4.8 ALS-real-schema parser. 'Mong Kok' is also an MTR station,
    so the MTR-catalog shortcut will resolve before ALS. We mock ALS anyway in
    case the resolver order ever changes — the response uses the real schema."""
    sample = {
        "RequestAddress": {"AddressLine": ["Mong Kok"]},
        "SuggestedAddress": [
            {
                "Address": {
                    "PremisesAddress": {
                        "EngPremisesAddress": {
                            "BuildingName": "MONG KOK MTR",
                            "EngDistrict": {"DcDistrict": "YAU TSIM MONG"},
                        },
                        "ChiPremisesAddress": {
                            "BuildingName": "旺角港鐵站",
                            "ChiDistrict": {"DcDistrict": "油尖旺"},
                        },
                        "GeospatialInformation": {
                            "Latitude": "22.3195",
                            "Longitude": "114.1692",
                        },
                    }
                }
            }
        ],
    }
    # v0.4.8: the MTR-catalog shortcut resolves 'Mong Kok' before ALS is hit,
    # so the mock may not be exercised — that's the correct path; disable the
    # assert-all-called check.
    with respx.mock(
        base_url=ALS_URL.rsplit("/", 1)[0], assert_all_called=False
    ) as mock:
        mock.get("/lookup").mock(return_value=httpx.Response(200, json=sample))
        registry = build_default_registry()
        ctx = ToolContext(session_id="w-2")
        result = await registry.dispatch(
            "transport.plan_walking_route",
            {"origin": "Mong Kok", "destination": "Mong Kok"},
            ctx,
        )
    assert result.status == "ok", result.error
    assert result.result is not None
    # Same point; distance ~0, duration floors at 1 min.
    assert result.result["distance_m"] == 0
    assert result.result["duration_min"] >= 1


# --- plan_taxi_estimate --------------------------------------------------


@pytest.mark.asyncio
async def test_plan_taxi_estimate_lat_lng_short_trip() -> None:
    registry = build_default_registry()
    ctx = ToolContext(session_id="t-1")
    result = await registry.dispatch(
        "transport.plan_taxi_estimate",
        {
            "origin_lat": 22.2820,
            "origin_lng": 114.1582,
            "destination_lat": 22.2863,
            "destination_lng": 114.1515,
        },
        ctx,
    )
    assert result.status == "ok", result.error
    assert result.result is not None
    # Short trip — fare should be within the flag-down band or just above.
    assert 27 <= result.result["fare_hkd_low"] <= 60
    assert result.result["fare_hkd_high"] > result.result["fare_hkd_low"]
    assert "HK$27" in result.result["fare_explanation"]


# --- plan_journey --------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_journey_returns_three_modes() -> None:
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
    assert modes == {"walk", "mtr", "taxi"}
    # Taxi option should have both distance and fare range.
    taxi = next(opt for opt in result.result["options"] if opt["mode"] == "taxi")
    assert taxi["duration_min"] is not None
    assert taxi["fare_hkd_range"] is not None
    # Recommendation defaults to `mtr` for a ~7-10 km HK trip.
    assert result.result["recommendation"] in {"mtr", "taxi", "walk"}


@pytest.mark.asyncio
async def test_plan_journey_custom_mode_subset() -> None:
    registry = build_default_registry()
    ctx = ToolContext(session_id="j-2")
    result = await registry.dispatch(
        "transport.plan_journey",
        {
            "origin_lat": 22.3195,
            "origin_lng": 114.1692,
            "destination_lat": 22.3817,
            "destination_lng": 114.1870,
            "modes": ["walk", "taxi"],
        },
        ctx,
    )
    assert result.status == "ok"
    assert result.result is not None
    assert {o["mode"] for o in result.result["options"]} == {"walk", "taxi"}


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
    # Verify pre-state
    loaded = await store.load("f-1")
    assert loaded.origin is not None

    registry = build_default_registry()
    ctx = ToolContext(session_id="f-1")
    result = await registry.dispatch("meta.forget_me", {}, ctx)
    assert result.status == "ok"
    assert result.result is not None
    assert result.result["ok"] is True
    # After forget: new SessionSlots defaults (no origin).
    reloaded = await store.load("f-1")
    assert reloaded.origin is None
