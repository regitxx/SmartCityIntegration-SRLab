"""Tool registry + individual tool unit tests (mocked HTTP)."""
# ruff: noqa: RUF001  # Cantonese / CJK punctuation is intentional.

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from smcity.tools import build_default_registry
from smcity.tools.geo import ALS_URL
from smcity.tools.registry import ToolContext, ToolValidationError
from smcity.tools.transport import MTR_NEXT_TRAIN_URL, resolve_mtr_station


def test_registry_exposes_expected_tools() -> None:
    registry = build_default_registry()
    names = registry.names()
    expected = {
        "transport.get_mtr_next_trains",
        "geo.address_lookup",
        "context.get_current_weather",
        "context.get_active_warnings",
        "context.get_aqhi",
        "meta.ask_user",
        "meta.what_languages_are_supported",
    }
    assert expected.issubset(set(names))


def test_registry_schemas_are_openai_shape() -> None:
    registry = build_default_registry()
    for schema in registry.openai_schemas():
        assert schema["type"] == "function"
        assert "name" in schema["function"]
        assert "parameters" in schema["function"]


def test_mtr_station_fuzzy_lookup_handles_all_three_scripts() -> None:
    for name, code in [
        ("Sheung Wan", "SHW"),
        ("上環", "SHW"),
        ("上环", "SHW"),
        ("Sha Tin", "SHT"),
        # deliberate typo
        ("Shueng Wan", "SHW"),
    ]:
        station = resolve_mtr_station(name)
        assert station is not None, f"no match for {name!r}"
        assert station.code == code


def test_unknown_tool_raises_validation_error() -> None:
    registry = build_default_registry()
    with pytest.raises(ToolValidationError):
        registry.get("nope.does_not_exist")


@pytest.mark.asyncio
@respx.mock
async def test_als_address_lookup_parses_real_schema() -> None:
    """Real ALS schema — SuggestedAddress[]/Address/PremisesAddress/...,
    NOT GeoJSON features[]. Pre-v0.4.8 the parser checked the wrong path
    and silently returned 0 candidates for every query."""
    sample: dict[str, Any] = {
        "RequestAddress": {"AddressLine": ["Sheung Wan"]},
        "SuggestedAddress": [
            {
                "Address": {
                    "PremisesAddress": {
                        "EngPremisesAddress": {
                            "BuildingName": "SHEUNG WAN MTR STATION",
                            "EngStreet": {
                                "StreetName": "DES VOEUX ROAD CENTRAL",
                                "BuildingNoFrom": "1",
                            },
                            "EngDistrict": {"DcDistrict": "CENTRAL AND WESTERN DISTRICT"},
                            "Region": "HK",
                        },
                        "ChiPremisesAddress": {
                            "BuildingName": "上環港鐵站",
                            "ChiStreet": {"StreetName": "德輔道中", "BuildingNoFrom": "1"},
                            "ChiDistrict": {"DcDistrict": "中西區"},
                            "Region": "港島",
                        },
                        "GeoAddress": "x",
                        "GeospatialInformation": {
                            "Northing": "816000",
                            "Easting": "834000",
                            "Latitude": "22.2863",
                            "Longitude": "114.1515",
                        },
                    }
                },
                "ValidationInformation": {"Score": 95.0},
            }
        ],
    }
    respx.get(ALS_URL).mock(return_value=httpx.Response(200, json=sample))
    registry = build_default_registry()

    ctx = ToolContext(session_id="t", locale="eng", query_lang="en", translation_applied=False)
    result = await registry.dispatch("geo.address_lookup", {"query": "Sheung Wan"}, ctx)
    assert result.status == "ok"
    assert result.result is not None
    assert result.result["candidates"], "v0.4.8: real ALS schema must produce candidates"
    cand = result.result["candidates"][0]
    assert cand["name_en"] == "SHEUNG WAN MTR STATION"
    assert cand["name_tc"] == "上環港鐵站"
    assert cand["lat"] == pytest.approx(22.2863, rel=1e-4)
    assert cand["lng"] == pytest.approx(114.1515, rel=1e-4)
    assert "Central and Western" in (cand["district"] or "").lower() or "CENTRAL AND WESTERN" in (
        cand["district"] or ""
    )


@pytest.mark.asyncio
@respx.mock
async def test_mtr_tool_parses_next_trains() -> None:
    payload = {
        "status": 1,
        "message": "",
        "data": {
            "ISL-SHW": {
                "UP": [
                    {"dest": "CHW", "ttnt": "2", "plat": "1", "seq": 1},
                    {"dest": "CHW", "ttnt": "5", "plat": "1", "seq": 2},
                ],
                "DOWN": [
                    {"dest": "KET", "ttnt": "3", "plat": "2", "seq": 1},
                ],
            }
        },
    }
    respx.get(MTR_NEXT_TRAIN_URL).mock(return_value=httpx.Response(200, json=payload))

    registry = build_default_registry()
    ctx = ToolContext(session_id="t", locale="yue", query_lang="zh-Hant", translation_applied=True)
    result = await registry.dispatch("transport.get_mtr_next_trains", {"station_name": "上環"}, ctx)
    assert result.status == "ok"
    assert result.result is not None
    trains = result.result["next_trains"]
    assert len(trains) == 3
    assert {t["direction"] for t in trains} == {"UP", "DOWN"}


@pytest.mark.asyncio
async def test_ask_user_tool_round_trips_question() -> None:
    registry = build_default_registry()
    ctx = ToolContext(session_id="t")
    result = await registry.dispatch(
        "meta.ask_user",
        {"question": "搭 MTR 定巴士？", "slot": "mode"},
        ctx,
    )
    assert result.status == "ok"
    assert result.result is not None
    assert result.result["slot"] == "mode"
    assert "MTR" in result.result["question"]


# --- v0.6.0 POI collapse: regression guards -------------------------------


def test_registry_exposes_collapsed_find_poi_only() -> None:
    """v0.6.0 collapsed the 30 per-category POI tools into one geo.find_poi.
    Registry must expose the new tool and NOT the old per-slug names."""
    registry = build_default_registry()
    names = set(registry.names())
    assert "geo.find_poi" in names
    legacy = {
        f"geo.find_{slug}"
        for slug in (
            "dentist",
            "convenience_store",
            "public_toilet",
            "bench",
            "mtr_station_entrance",
        )
    }
    assert not (legacy & names), f"legacy POI tool names still registered: {legacy & names}"


def test_find_poi_category_literal_rejects_unknown_slug() -> None:
    """`category` is a Literal — pydantic must reject any slug that isn't in
    the enum. Keeps the LLM from inventing categories that would blow up
    in Overpass query construction."""
    from pydantic import ValidationError

    from smcity.tools.osm_pois import FindPoiArgs

    with pytest.raises(ValidationError):
        FindPoiArgs.model_validate({"category": "fictional_category", "lat": 22.3, "lng": 114.17})


def test_find_poi_category_required() -> None:
    """`category` has no default — pydantic must reject a missing one."""
    from pydantic import ValidationError

    from smcity.tools.osm_pois import FindPoiArgs

    with pytest.raises(ValidationError):
        FindPoiArgs.model_validate({"lat": 22.3, "lng": 114.17})


def test_registry_schema_size_is_bounded() -> None:
    """v0.6.0 collapse must keep the total OpenAI-schema blob under 30 KB.
    Pre-collapse it was ~66 KB (mostly 30 POI tools duplicating FindPoiArgs).
    A regression here likely means a new tool with a giant args schema —
    fix at the source rather than raising the cap."""
    import json as _json

    registry = build_default_registry()
    blob = _json.dumps(registry.openai_schemas(), ensure_ascii=False)
    assert len(blob) < 30_000, (
        f"tool schema blob is {len(blob)} chars; v0.6.0 budget is 30K. "
        "If you intentionally added a large tool, raise the cap and update "
        "this comment with the new ceiling."
    )
