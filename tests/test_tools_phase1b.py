"""Unit tests for Phase 1b tools — facility, housing, KMB, Citybus, cross-stop search."""

from __future__ import annotations

import httpx
import pytest
import respx

from smcity.tools import build_default_registry
from smcity.tools.registry import ToolContext
from smcity.tools.transport_citybus import CITYBUS_BASE
from smcity.tools.transport_kmb import KMB_BASE


def test_registry_now_has_phase1b_tools() -> None:
    registry = build_default_registry()
    names = set(registry.names())
    for expected in (
        "transport.get_kmb_eta_by_stop",
        "transport.get_kmb_eta_by_route_stop",
        "transport.get_citybus_eta_by_route_stop",
        "transport.get_citybus_route_stops",
        "transport.find_stops_near_point",
        "transport.find_stops_by_name",
        "facility.find_nearby_courts",
        "facility.find_nearby_pools",
        "housing.get_estate_info",
        "housing.list_estates_in_district",
    ):
        assert expected in names, f"missing tool {expected}"


@pytest.mark.asyncio
async def test_find_nearby_courts_by_coords() -> None:
    registry = build_default_registry()
    ctx = ToolContext(session_id="t", locale="eng", query_lang="en")
    # Sheung Wan coordinates
    result = await registry.dispatch(
        "facility.find_nearby_courts",
        {"lat": 22.2863, "lng": 114.1515, "radius_km": 2.0, "max_results": 3},
        ctx,
    )
    assert result.status == "ok", result.error
    assert result.result is not None
    courts = result.result["courts"]
    assert 1 <= len(courts) <= 3
    for c in courts:
        assert c["distance_m"] is not None
        assert c["distance_m"] <= 2000


@pytest.mark.asyncio
async def test_find_nearby_courts_by_district() -> None:
    registry = build_default_registry()
    ctx = ToolContext(session_id="t")
    result = await registry.dispatch("facility.find_nearby_courts", {"district": "Sha Tin"}, ctx)
    assert result.status == "ok"
    assert result.result is not None
    assert result.result["courts"], "expected at least one court in Sha Tin"
    for c in result.result["courts"]:
        assert "Sha Tin" in c["district"]


@pytest.mark.asyncio
async def test_find_nearby_courts_by_name_query() -> None:
    registry = build_default_registry()
    ctx = ToolContext(session_id="t")
    result = await registry.dispatch("facility.find_nearby_courts", {"name_query": "Southorn"}, ctx)
    assert result.status == "ok"
    assert result.result is not None
    assert any("Southorn" in c["name_en"] for c in result.result["courts"])


@pytest.mark.asyncio
async def test_pools_filter_indoor_only() -> None:
    registry = build_default_registry()
    ctx = ToolContext(session_id="t")
    result = await registry.dispatch("facility.find_nearby_pools", {"indoor_only": True}, ctx)
    assert result.status == "ok"
    assert result.result is not None
    assert all(p["indoor"] is True for p in result.result["pools"])


@pytest.mark.asyncio
async def test_housing_estate_info_fuzzy() -> None:
    registry = build_default_registry()
    ctx = ToolContext(session_id="t", locale="yue", query_lang="zh-Hant")
    for needle, expected_id in [
        ("Choi Hung", "CHOI"),
        ("彩虹", "CHOI"),
        ("Mei Foo", "MEIF"),
        ("美孚", "MEIF"),
    ]:
        result = await registry.dispatch("housing.get_estate_info", {"name": needle}, ctx)
        assert result.status == "ok"
        assert result.result is not None
        match = result.result.get("match")
        assert match is not None, f"no match for {needle}"
        assert match["id"] == expected_id


@pytest.mark.asyncio
async def test_housing_list_estates_in_district() -> None:
    registry = build_default_registry()
    ctx = ToolContext(session_id="t")
    result = await registry.dispatch(
        "housing.list_estates_in_district", {"district": "Wong Tai Sin"}, ctx
    )
    assert result.status == "ok"
    assert result.result is not None
    ids = {e["id"] for e in result.result["estates"]}
    assert "CHOI" in ids or "CHUK" in ids


@pytest.mark.asyncio
@respx.mock
async def test_kmb_eta_by_stop_parses_live_schema() -> None:
    # Stub the stop catalog (6715 entries in prod; we give two).
    respx.get(f"{KMB_BASE}/stop").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "stop": "18492910339410B1",
                        "name_en": "CHUK YUEN ESTATE BUS TERMINUS",
                        "name_tc": "竹園邨總站",
                        "name_sc": "竹园邨总站",
                        "lat": "22.345415",
                        "long": "114.192640",
                    },
                    {
                        "stop": "ABCDEF1234567890",
                        "name_en": "SHEUNG WAN MTR",
                        "name_tc": "上環港鐵站",
                        "name_sc": "上环港铁站",
                        "lat": "22.2863",
                        "long": "114.1515",
                    },
                ]
            },
        )
    )
    respx.get(f"{KMB_BASE}/stop-eta/ABCDEF1234567890").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "co": "KMB",
                        "route": "1",
                        "dir": "O",
                        "service_type": 1,
                        "seq": 1,
                        "dest_tc": "尖沙咀碼頭",
                        "dest_en": "STAR FERRY",
                        "eta_seq": 1,
                        "eta": "2026-04-21T18:45:00+08:00",
                        "rmk_en": "Scheduled Bus",
                    }
                ]
            },
        )
    )
    # NOTE: the KMB catalog is module-level cached. Reset to force re-load.
    from smcity.tools import transport_kmb as mod

    mod._catalog = mod._StopCatalog()

    registry = build_default_registry()
    ctx = ToolContext(session_id="t", locale="yue", query_lang="zh-Hant")
    result = await registry.dispatch(
        "transport.get_kmb_eta_by_stop", {"stop_name_or_id": "Sheung Wan MTR"}, ctx
    )
    assert result.status == "ok", result.error
    assert result.result is not None
    assert result.result["stop_name_en"] == "SHEUNG WAN MTR"
    assert result.result["etas"][0]["route"] == "1"


@pytest.mark.asyncio
@respx.mock
async def test_citybus_eta_by_route_stop() -> None:
    respx.get(f"{CITYBUS_BASE}/eta/CTB/001028/1").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "co": "CTB",
                        "route": "1",
                        "dir": "I",
                        "eta": "2026-04-21T18:50:00+08:00",
                        "dest_en": "Happy Valley",
                        "dest_tc": "跑馬地",
                        "dest_sc": "跑马地",
                    }
                ]
            },
        )
    )
    registry = build_default_registry()
    ctx = ToolContext(session_id="t", locale="eng", query_lang="en")
    result = await registry.dispatch(
        "transport.get_citybus_eta_by_route_stop",
        {"route": "1", "stop_id": "001028"},
        ctx,
    )
    assert result.status == "ok", result.error
    assert result.result is not None
    assert result.result["etas"][0]["destination_en"] == "Happy Valley"


@pytest.mark.asyncio
@respx.mock
async def test_find_stops_near_point_kmb_plus_mtr() -> None:
    respx.get(f"{KMB_BASE}/stop").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "stop": "AAAA111122223333",
                        "name_en": "ADMIRALTY MTR STATION",
                        "name_tc": "金鐘站",
                        "name_sc": "金钟站",
                        "lat": "22.2797",
                        "long": "114.1648",
                    },
                    {
                        "stop": "BBBB111122223333",
                        "name_en": "SHEUNG WAN BUS TERMINUS",
                        "name_tc": "上環巴士總站",
                        "name_sc": "上环巴士总站",
                        "lat": "22.2863",
                        "long": "114.1515",
                    },
                    {
                        "stop": "CCCC111122223333",
                        "name_en": "SHATIN BUS TERMINUS",
                        "name_tc": "沙田巴士總站",
                        "name_sc": "沙田巴士总站",
                        "lat": "22.3817",
                        "long": "114.1870",
                    },
                ]
            },
        )
    )
    from smcity.tools import transport_kmb as mod

    mod._catalog = mod._StopCatalog()

    registry = build_default_registry()
    ctx = ToolContext(session_id="t", locale="eng", query_lang="en")
    # Sheung Wan MTR coords
    result = await registry.dispatch(
        "transport.find_stops_near_point",
        {"lat": 22.2863, "lng": 114.1515, "radius_m": 800, "max_results": 5},
        ctx,
    )
    assert result.status == "ok", result.error
    assert result.result is not None
    ops = {s["operator"] for s in result.result["stops"]}
    # Should include KMB Sheung Wan and MTR Sheung Wan (SHW) within 800 m.
    assert "kmb" in ops
    assert "mtr" in ops
