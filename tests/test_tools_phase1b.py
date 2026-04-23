# ruff: noqa: RUF001
"""Unit tests for Phase 1b tools — facility, housing, KMB, Citybus, cross-stop search."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from smcity.tools import build_default_registry
from smcity.tools import facility as facility_mod
from smcity.tools import housing as housing_mod
from smcity.tools.csdi import CSDI_DATASETS
from smcity.tools.housing import HKHA_LIVE_URL
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


# --- facility test fixtures: mock CSDI FeatureServer responses -------------

_COURTS_URL = CSDI_DATASETS["lcsd_basketball_courts"].url + "/query"
_POOLS_URL = CSDI_DATASETS["lcsd_swimming_pools"].url + "/query"


def _court_feature(
    oid: int,
    name_en: str,
    name_tc: str,
    address_en: str,
    district: str,
    courts: int,
    lng: float,
    lat: float,
) -> dict[str, Any]:
    return {
        "attributes": {
            "OBJECTID": oid,
            "NAME_EN": name_en,
            "NAME_TC": name_tc,
            "ADDRESS_EN": address_en,
            "ADDRESS_TC": "",
            "SEARCH01_EN": district,
            "No__of_Basketball_Courts_EN": courts,
        },
        "geometry": {"x": lng, "y": lat},
    }


def _pool_feature(
    oid: int,
    name_en: str,
    name_tc: str,
    district_en: str,
    lng: float,
    lat: float,
    facility_type: str = "SWIMMING POOLS",
) -> dict[str, Any]:
    return {
        "attributes": {
            "OBJECTID": oid,
            "NameEN": name_en,
            "NameTC": name_tc,
            "AddressEN": "",
            "AddressTC": "",
            "DistrictEN": district_en,
            "FacilityTypeEN": facility_type,
            "OpeningHoursEN": "",
            "TelephoneEN": "",
        },
        "geometry": {"x": lng, "y": lat},
    }


@pytest.fixture
def mock_csdi_facility() -> Any:
    """Patch the CSDI HTTP endpoints + reset facility catalog caches."""
    facility_mod._reset_catalogs_for_tests()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_COURTS_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "features": [
                        _court_feature(
                            1,
                            "Southorn Playground",
                            "修頓遊樂場",
                            "111 Hennessy Road, Wan Chai",
                            "WAN CHAI",
                            2,
                            114.1728,
                            22.2773,
                        ),
                        _court_feature(
                            2,
                            "Sha Tin Sports Ground",
                            "沙田運動場",
                            "Shan Mei Street, Sha Tin",
                            "SHA TIN",
                            3,
                            114.19,
                            22.3817,
                        ),
                        _court_feature(
                            3,
                            "Sheung Wan Sports Centre",
                            "上環體育館",
                            "Sheung Wan, HK",
                            "CENTRAL AND WESTERN",
                            1,
                            114.1515,
                            22.2863,
                        ),
                        # sport ground with 0 basketball courts — should be filtered out
                        _court_feature(
                            4,
                            "Non-Basketball Venue",
                            "非籃球場",
                            "Somewhere",
                            "NORTH",
                            0,
                            114.2,
                            22.5,
                        ),
                    ],
                    "exceededTransferLimit": False,
                },
            )
        )
        mock.get(_POOLS_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "features": [
                        _pool_feature(
                            1,
                            "Victoria Park Swimming Pool",
                            "維多利亞公園游泳池",
                            "Wan Chai",
                            114.1868,
                            22.2821,
                        ),
                        _pool_feature(
                            2, "Tuen Mun Swimming Pool", "屯門游泳池", "Tuen Mun", 113.9631, 22.383
                        ),
                        _pool_feature(
                            3,
                            "Morrison Hill Swimming Pool",
                            "摩利臣山游泳池",
                            "Wan Chai",
                            114.1744,
                            22.2755,
                        ),
                    ],
                    "exceededTransferLimit": False,
                },
            )
        )
        yield mock
    facility_mod._reset_catalogs_for_tests()


@pytest.mark.asyncio
async def test_find_nearby_courts_by_coords(mock_csdi_facility: Any) -> None:
    registry = build_default_registry()
    ctx = ToolContext(session_id="t", locale="eng", query_lang="en")
    # Sheung Wan coordinates
    result = await registry.dispatch(
        "facility.find_nearby_courts",
        {"lat": 22.2863, "lng": 114.1515, "radius_km": 5.0, "max_results": 3},
        ctx,
    )
    assert result.status == "ok", result.error
    assert result.result is not None
    courts = result.result["courts"]
    assert 1 <= len(courts) <= 3
    for c in courts:
        assert c["distance_m"] is not None
        assert c["distance_m"] <= 5000


@pytest.mark.asyncio
async def test_find_nearby_courts_by_district(mock_csdi_facility: Any) -> None:
    registry = build_default_registry()
    ctx = ToolContext(session_id="t")
    result = await registry.dispatch("facility.find_nearby_courts", {"district": "Sha Tin"}, ctx)
    assert result.status == "ok"
    assert result.result is not None
    assert result.result["courts"], "expected at least one court in Sha Tin"
    for c in result.result["courts"]:
        # CSDI districts are uppercase; adapter title-cases them back.
        assert "Sha Tin" in (c["district"] or "")


@pytest.mark.asyncio
async def test_find_nearby_courts_by_name_query(mock_csdi_facility: Any) -> None:
    registry = build_default_registry()
    ctx = ToolContext(session_id="t")
    result = await registry.dispatch("facility.find_nearby_courts", {"name_query": "Southorn"}, ctx)
    assert result.status == "ok"
    assert result.result is not None
    assert any("Southorn" in c["name_en"] for c in result.result["courts"])


@pytest.mark.asyncio
async def test_courts_filters_zero_basketball_venues(mock_csdi_facility: Any) -> None:
    """Sport grounds with 0 basketball courts must be dropped from results."""
    registry = build_default_registry()
    ctx = ToolContext(session_id="t")
    result = await registry.dispatch("facility.find_nearby_courts", {"max_results": 20}, ctx)
    assert result.status == "ok"
    assert result.result is not None
    names = [c["name_en"] for c in result.result["courts"]]
    assert "Non-Basketball Venue" not in names


@pytest.mark.asyncio
async def test_pools_by_district(mock_csdi_facility: Any) -> None:
    registry = build_default_registry()
    ctx = ToolContext(session_id="t")
    result = await registry.dispatch("facility.find_nearby_pools", {"district": "Wan Chai"}, ctx)
    assert result.status == "ok"
    assert result.result is not None
    assert result.result["pools"], "expected Wan Chai pools"
    for p in result.result["pools"]:
        assert "Wan Chai" in (p["district"] or "")


@pytest.mark.asyncio
async def test_pools_name_query(mock_csdi_facility: Any) -> None:
    registry = build_default_registry()
    ctx = ToolContext(session_id="t")
    result = await registry.dispatch("facility.find_nearby_pools", {"name_query": "Victoria"}, ctx)
    assert result.status == "ok"
    assert result.result is not None
    assert any("Victoria" in p["name_en"] for p in result.result["pools"])


# --- housing tests: mock the Housing Authority live JSON API -------------


def _hkha_row(
    name_en: str,
    district: str,
    region: str,
    lat: str,
    lng: str,
    *,
    estate_type: str = "Public Rental Housing",
    blocks: str = "10",
    flats: str = "5 000 as at 31.12.2025",
    flat_size: str = "14 – 40",
    year: str = "1990",
) -> dict[str, Any]:
    return {
        "Estate_Name": name_en,
        "District_Name": district,
        "Region_Name": region,
        "Map_Latitude": lat,
        "Map_Longitude": lng,
        "Type_of_Estate": estate_type,
        "Year_of_Intake": year,
        "No_of_Blocks": blocks,
        "No_of_Rental_Flats": flats,
        "Flat_Size_m2": flat_size,
        "Estate_Website": "",
    }


@pytest.fixture
def mock_hkha_live() -> Any:
    """Patch the HKHA live JSON endpoint + reset the module-level cache."""
    housing_mod._reset_catalog_for_tests()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(HKHA_LIVE_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        _hkha_row(
                            "Choi Hung Estate",
                            "Wong Tai Sin",
                            "Kowloon",
                            "22.3348",
                            "114.2087",
                            blocks="11",
                            flats="7 455 as at 31.12.2025",
                        ),
                        _hkha_row(
                            "Mei Foo Sun Chuen",
                            "Sham Shui Po",
                            "Kowloon",
                            "22.3375",
                            "114.1378",
                            estate_type="Home Ownership Scheme",
                            blocks="99",
                            flats="13 149",
                        ),
                        _hkha_row(
                            "Shek Kip Mei Estate", "Sham Shui Po", "Kowloon", "22.3331", "114.1683"
                        ),
                        _hkha_row(
                            "Tak Long Estate", "Kowloon City", "Kowloon", "22.330105", "114.2031"
                        ),
                        # No-TC-map entry: EN-only fuzzy match only.
                        _hkha_row(
                            "Happy Fake Estate",
                            "Central & Western",
                            "Hong Kong Island",
                            "22.2820",
                            "114.1582",
                        ),
                    ]
                },
            )
        )
        yield mock
    housing_mod._reset_catalog_for_tests()


@pytest.mark.asyncio
async def test_housing_estate_info_fuzzy_en_and_tc(mock_hkha_live: Any) -> None:
    registry = build_default_registry()
    ctx = ToolContext(session_id="t", locale="yue", query_lang="zh-Hant")
    for needle, expected_name in [
        ("Choi Hung", "Choi Hung Estate"),
        ("彩虹", "Choi Hung Estate"),  # via TC overlay
        ("Mei Foo", "Mei Foo Sun Chuen"),
        ("美孚", "Mei Foo Sun Chuen"),  # via TC overlay
        ("Tak Long", "Tak Long Estate"),
        ("Happy Fake", "Happy Fake Estate"),  # EN-only, still matches
    ]:
        result = await registry.dispatch("housing.get_estate_info", {"name": needle}, ctx)
        assert result.status == "ok", result.error
        assert result.result is not None
        match = result.result.get("match")
        assert match is not None, f"no match for {needle}"
        assert match["name_en"] == expected_name


@pytest.mark.asyncio
async def test_housing_parses_numeric_with_trailing_text(mock_hkha_live: Any) -> None:
    registry = build_default_registry()
    ctx = ToolContext(session_id="t")
    result = await registry.dispatch("housing.get_estate_info", {"name": "Choi Hung"}, ctx)
    assert result.status == "ok"
    assert result.result is not None
    match = result.result["match"]
    assert match is not None
    assert match["flats"] == 7455  # parsed past the "as at ..." suffix
    assert "as at" in match["flats_raw"]
    assert match["blocks"] == 11


@pytest.mark.asyncio
async def test_housing_list_estates_in_district(mock_hkha_live: Any) -> None:
    registry = build_default_registry()
    ctx = ToolContext(session_id="t")
    result = await registry.dispatch(
        "housing.list_estates_in_district", {"district": "Sham Shui Po"}, ctx
    )
    assert result.status == "ok"
    assert result.result is not None
    names = {e["name_en"] for e in result.result["estates"]}
    assert "Mei Foo Sun Chuen" in names
    assert "Shek Kip Mei Estate" in names


@pytest.mark.asyncio
async def test_housing_region_filter(mock_hkha_live: Any) -> None:
    registry = build_default_registry()
    ctx = ToolContext(session_id="t")
    result = await registry.dispatch(
        "housing.list_estates_in_district",
        {"district": "Central", "region": "Hong Kong Island"},
        ctx,
    )
    assert result.status == "ok"
    assert result.result is not None
    # Region filter is substring — "Hong Kong Island" matches "Hong Kong Island".
    assert any(e["name_en"] == "Happy Fake Estate" for e in result.result["estates"])


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
