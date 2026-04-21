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
async def test_als_address_lookup_parses_features() -> None:
    sample: dict[str, Any] = {
        "features": [
            {
                "properties": {
                    "EngPremisesAddress": "Sheung Wan MTR",
                    "ChiPremisesAddress": "上環港鐵站",
                    "district": "Central & Western",
                    "lat": 22.2863,
                    "lng": 114.1515,
                },
                "geometry": {"type": "Point", "coordinates": [114.1515, 22.2863]},
            }
        ]
    }
    respx.get(ALS_URL).mock(return_value=httpx.Response(200, json=sample))
    registry = build_default_registry()

    ctx = ToolContext(session_id="t", locale="eng", query_lang="en", translation_applied=False)
    result = await registry.dispatch("geo.address_lookup", {"query": "Sheung Wan"}, ctx)
    assert result.status == "ok"
    assert result.result is not None
    assert result.result["candidates"][0]["name_en"] == "Sheung Wan MTR"
    assert result.result["candidates"][0]["lat"] == pytest.approx(22.2863, rel=1e-4)


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
