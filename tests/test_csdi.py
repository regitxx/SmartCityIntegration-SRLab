"""Tests for the CSDI ArcGIS FeatureServer client + generic tool."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from smcity.tools.csdi import (
    CSDI_DATASETS,
    CSDI_QUERY_TOOL,
    ArcGisQueryResult,
    CSDIDataset,
    CSDIQueryArgs,
    query_feature_server,
    register_dataset,
)
from smcity.tools.registry import ToolContext, ToolUpstreamError


def _feat(oid: int, *, name_en: str, name_tc: str, x: float, y: float) -> dict[str, Any]:
    return {
        "attributes": {"OBJECTID": oid, "NAME_EN": name_en, "NAME_TC": name_tc},
        "geometry": {"x": x, "y": y},
    }


@pytest.fixture(autouse=True)
def _reset_csdi_registry() -> Any:
    """Each test starts with a clean registry; production datasets are restored after."""
    saved = dict(CSDI_DATASETS)
    CSDI_DATASETS.clear()
    yield
    CSDI_DATASETS.clear()
    CSDI_DATASETS.update(saved)


@pytest.mark.asyncio
@respx.mock
async def test_query_feature_server_single_page() -> None:
    url = "https://example.test/rest/services/Demo/FeatureServer/0"
    respx.get(f"{url}/query").mock(
        return_value=httpx.Response(
            200,
            json={
                "features": [
                    _feat(1, name_en="A", name_tc="甲", x=114.16, y=22.28),
                    _feat(2, name_en="B", name_tc="乙", x=114.18, y=22.29),
                ],
                "exceededTransferLimit": False,
            },
        )
    )
    result: ArcGisQueryResult = await query_feature_server(url)
    assert result.total == 2
    assert result.truncated is False
    assert result.features[0].lat == pytest.approx(22.28)
    assert result.features[0].lng == pytest.approx(114.16)
    assert result.features[0].attributes["NAME_EN"] == "A"


@pytest.mark.asyncio
@respx.mock
async def test_query_feature_server_follows_pagination() -> None:
    url = "https://example.test/rest/services/Demo/FeatureServer/0"
    # First page signals truncation; second page does not.
    route = respx.get(f"{url}/query")
    route.side_effect = [
        httpx.Response(
            200,
            json={
                "features": [_feat(i, name_en=f"N{i}", name_tc="x", x=114, y=22) for i in range(2)],
                "exceededTransferLimit": True,
            },
        ),
        httpx.Response(
            200,
            json={
                "features": [
                    _feat(i, name_en=f"N{i}", name_tc="x", x=114, y=22) for i in range(2, 3)
                ],
                "exceededTransferLimit": False,
            },
        ),
    ]
    result = await query_feature_server(url, page_size=2, limit=10)
    assert result.total == 3
    # Two pages fetched.
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_query_feature_server_limit_caps_results() -> None:
    url = "https://example.test/rest/services/Demo/FeatureServer/0"
    respx.get(f"{url}/query").mock(
        return_value=httpx.Response(
            200,
            json={
                "features": [_feat(i, name_en=f"N{i}", name_tc="x", x=114, y=22) for i in range(5)],
                "exceededTransferLimit": True,
            },
        )
    )
    result = await query_feature_server(url, page_size=5, limit=3)
    assert result.total == 3  # truncated by caller-side limit
    assert result.truncated is True


@pytest.mark.asyncio
@respx.mock
async def test_query_feature_server_raises_on_arcgis_error() -> None:
    url = "https://example.test/rest/services/Demo/FeatureServer/0"
    respx.get(f"{url}/query").mock(
        return_value=httpx.Response(
            200,
            json={"error": {"code": 400, "message": "Invalid WHERE clause"}},
        )
    )
    with pytest.raises(ToolUpstreamError, match="Invalid WHERE clause"):
        await query_feature_server(url)


@pytest.mark.asyncio
@respx.mock
async def test_query_feature_server_raises_on_http_error() -> None:
    url = "https://example.test/rest/services/Demo/FeatureServer/0"
    respx.get(f"{url}/query").mock(return_value=httpx.Response(500))
    with pytest.raises(ToolUpstreamError):
        await query_feature_server(url)


@pytest.mark.asyncio
@respx.mock
async def test_query_feature_server_passes_bbox() -> None:
    url = "https://example.test/rest/services/Demo/FeatureServer/0"
    route = respx.get(f"{url}/query").mock(
        return_value=httpx.Response(200, json={"features": [], "exceededTransferLimit": False})
    )
    await query_feature_server(url, bbox=(114.0, 22.1, 114.5, 22.5))
    assert route.called
    request = route.calls.last.request
    assert "geometry=114.0%2C22.1%2C114.5%2C22.5" in str(
        request.url
    ) or "geometry=114.0,22.1,114.5,22.5" in str(request.url)
    assert "esriGeometryEnvelope" in str(request.url)


@pytest.mark.asyncio
async def test_csdi_tool_rejects_unknown_dataset() -> None:
    register_dataset(
        CSDIDataset(
            id="lcsd_basketball_courts",
            title_en="LCSD Basketball Courts",
            url="https://example.test/rest/services/LCSD/FeatureServer/0",
            name_field_en="NAME_EN",
            name_field_tc="NAME_TC",
            description="demo",
        )
    )
    with pytest.raises(ToolUpstreamError, match="unknown CSDI dataset"):
        await CSDI_QUERY_TOOL.handler(
            CSDIQueryArgs(dataset="not_a_dataset"),
            ToolContext(session_id="s"),
        )


def test_production_datasets_registered_on_import(
    _reset_csdi_registry: Any,
) -> None:
    """Importing the module should leave the two verified LCSD datasets ready."""
    # The autouse fixture has cleared the registry. Re-populate from the
    # module-level definitions by re-running the module initialiser.
    import importlib

    import smcity.tools.csdi as mod

    importlib.reload(mod)
    assert "lcsd_basketball_courts" in mod.CSDI_DATASETS
    assert "lcsd_swimming_pools" in mod.CSDI_DATASETS
    bb = mod.CSDI_DATASETS["lcsd_basketball_courts"]
    sp = mod.CSDI_DATASETS["lcsd_swimming_pools"]
    assert bb.url.startswith("https://portal.csdi.gov.hk/")
    assert sp.url.startswith("https://portal.csdi.gov.hk/")
    # Field-naming conventions differ between these two datasets; the
    # struct captures both so downstream code never guesses.
    assert bb.name_field_en == "NAME_EN"
    assert sp.name_field_en == "NameEN"


@pytest.mark.asyncio
@respx.mock
async def test_csdi_tool_round_trip_with_registered_dataset() -> None:
    url = "https://example.test/rest/services/LCSD/FeatureServer/0"
    register_dataset(
        CSDIDataset(
            id="lcsd_basketball_courts",
            title_en="LCSD Basketball Courts",
            url=url,
            name_field_en="NAME_EN",
            name_field_tc="NAME_TC",
            description="hero example",
        )
    )
    respx.get(f"{url}/query").mock(
        return_value=httpx.Response(
            200,
            json={
                "features": [
                    _feat(
                        1,
                        name_en="Sheung Wan Basketball Court",
                        name_tc="上環籃球場",
                        x=114.151,
                        y=22.286,
                    ),
                ],
                "exceededTransferLimit": False,
            },
        )
    )
    result = await CSDI_QUERY_TOOL.handler(
        CSDIQueryArgs(dataset="lcsd_basketball_courts", limit=5),
        ToolContext(session_id="s"),
    )
    assert result.total == 1
    assert result.features[0]["name_en"] == "Sheung Wan Basketball Court"
    assert result.features[0]["name_tc"] == "上環籃球場"
    assert result.features[0]["lat"] == pytest.approx(22.286)
