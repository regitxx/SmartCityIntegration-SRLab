"""Live data.gov.hk integration tests — skipped off-tailnet / offline."""

from __future__ import annotations

import socket

import pytest

from smcity.tools import build_default_registry
from smcity.tools.registry import ToolContext

pytestmark = pytest.mark.integration


def _net_ok(host: str, port: int = 443, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module")
def online() -> bool:
    return _net_ok("data.weather.gov.hk") and _net_ok("rt.data.gov.hk")


async def test_hko_weather_returns_real_temp(online: bool) -> None:
    if not online:
        pytest.skip("data.weather.gov.hk unreachable")
    registry = build_default_registry()
    ctx = ToolContext(session_id="i", locale="eng", query_lang="en")
    result = await registry.dispatch("context.get_current_weather", {}, ctx)
    assert result.status == "ok", result.error
    assert result.result is not None
    temp = result.result.get("temperature_c")
    # HK plausible range over the year
    assert temp is None or -5 <= float(temp) <= 50


async def test_mtr_sheung_wan_live(online: bool) -> None:
    if not online:
        pytest.skip("rt.data.gov.hk unreachable")
    registry = build_default_registry()
    ctx = ToolContext(session_id="i", locale="eng", query_lang="en")
    result = await registry.dispatch(
        "transport.get_mtr_next_trains", {"station_name": "Sheung Wan"}, ctx
    )
    assert result.status == "ok", result.error
    assert result.result is not None
    assert result.result["station_code"] == "SHW"


async def test_hko_warnings_endpoint_responds(online: bool) -> None:
    if not online:
        pytest.skip("data.weather.gov.hk unreachable")
    registry = build_default_registry()
    ctx = ToolContext(session_id="i", locale="eng", query_lang="en")
    result = await registry.dispatch("context.get_active_warnings", {}, ctx)
    assert result.status == "ok", result.error
    # May legitimately have 0 warnings; just assert structure.
    assert result.result is not None
    assert isinstance(result.result.get("warnings"), list)


async def test_als_resolves_sheung_wan(online: bool) -> None:
    if not _net_ok("www.als.gov.hk"):
        pytest.skip("als.gov.hk unreachable")
    registry = build_default_registry()
    ctx = ToolContext(session_id="i", locale="eng", query_lang="en")
    result = await registry.dispatch(
        "geo.address_lookup", {"query": "Sheung Wan", "max_results": 3}, ctx
    )
    # ALS sometimes returns empty for single-word queries; accept either.
    assert result.status == "ok", result.error


async def test_kmb_live_stop_catalog_and_eta(online: bool) -> None:
    if not _net_ok("data.etabus.gov.hk"):
        pytest.skip("data.etabus.gov.hk unreachable")
    # Reset the module-level catalog before hitting the live API.
    from smcity.tools import transport_kmb as mod

    mod._catalog = mod._StopCatalog()

    registry = build_default_registry()
    ctx = ToolContext(session_id="i", locale="eng", query_lang="en")
    result = await registry.dispatch(
        "transport.get_kmb_eta_by_stop", {"stop_name_or_id": "Sheung Wan"}, ctx
    )
    assert result.status == "ok", result.error
    assert result.result is not None
    # live schema keys
    assert "stop_id" in result.result
    assert "etas" in result.result
