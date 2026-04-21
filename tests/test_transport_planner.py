"""Unit tests for the simple walk + MTR planner."""

from __future__ import annotations

import pytest

from smcity.tools import build_default_registry
from smcity.tools.registry import ToolContext
from smcity.tools.transport_planner import _shortest_path, _topology


def test_topology_loads_all_major_lines() -> None:
    topo = _topology()
    # Every line we expect should contribute at least one station.
    for line in ("ISL", "TWL", "KTL", "EAL", "TKL", "SIL", "TCL", "AEL", "DRL", "TML"):
        assert any(line in topo.lines_of.get(st, set()) for st in topo.lines_of), (
            f"line {line} missing"
        )


def test_same_station_returns_trivial_path() -> None:
    path = _shortest_path("CEN", "CEN")
    assert path == [("CEN", None)]


def test_sheung_wan_to_sha_tin_hero_path() -> None:
    # SHW (ISL) → … → ADM (ISL↔EAL) → … → SHT (EAL).
    path = _shortest_path("SHW", "SHT")
    assert path is not None
    codes = [c for c, _ in path]
    assert codes[0] == "SHW"
    assert codes[-1] == "SHT"
    # Must pass through an interchange. Admiralty is the natural hop.
    assert "ADM" in codes
    # And must cover EAL for the Sha Tin leg.
    lines_used = {line for _, line in path if line}
    assert "ISL" in lines_used
    assert "EAL" in lines_used


def test_mong_kok_to_tsim_sha_tsui_single_line() -> None:
    path = _shortest_path("MOK", "TST")
    assert path is not None
    lines_used = {line for _, line in path if line}
    # TWL is direct — no interchange needed.
    assert lines_used == {"TWL"}


def test_unknown_station_returns_none() -> None:
    assert _shortest_path("SHW", "ZZZ") is None


@pytest.mark.asyncio
async def test_plan_tool_returns_structured_legs_sheung_wan_to_sha_tin() -> None:
    registry = build_default_registry()
    ctx = ToolContext(session_id="plan-1", locale="yue", query_lang="zh-Hant")
    result = await registry.dispatch(
        "transport.plan_simple_route",
        {"origin_station": "Sheung Wan", "destination_station": "Sha Tin"},
        ctx,
    )
    assert result.status == "ok", result.error
    assert result.result is not None
    assert result.result["ok"] is True
    assert result.result["origin_station"] == "SHW"
    assert result.result["destination_station"] == "SHT"
    assert result.result["total_duration_min"] and result.result["total_duration_min"] > 0
    # Should have at least one board + one ride + one alight leg.
    kinds = [leg["kind"] for leg in result.result["legs"]]
    assert "board" in kinds
    assert "ride" in kinds
    assert "alight" in kinds


@pytest.mark.asyncio
async def test_plan_tool_accepts_latlng_for_origin() -> None:
    registry = build_default_registry()
    ctx = ToolContext(session_id="plan-2", locale="eng", query_lang="en")
    # Sheung Wan harbourfront (near Macau ferry terminal) → Sha Tin.
    result = await registry.dispatch(
        "transport.plan_simple_route",
        {
            "origin_lat": 22.288,
            "origin_lng": 114.152,
            "destination_station": "Sha Tin",
        },
        ctx,
    )
    assert result.status == "ok", result.error
    assert result.result is not None
    assert result.result["ok"] is True
    # Origin should snap to SHW or a nearby station.
    assert result.result["origin_station"] in {"SHW", "CEN", "SYP"}


@pytest.mark.asyncio
async def test_plan_tool_reports_reason_when_missing_inputs() -> None:
    registry = build_default_registry()
    ctx = ToolContext(session_id="plan-3")
    result = await registry.dispatch(
        "transport.plan_simple_route", {"origin_station": "Sheung Wan"}, ctx
    )
    assert result.status == "ok"
    assert result.result is not None
    assert result.result["ok"] is False
    assert "destination" in (result.result.get("reason") or "").lower()
