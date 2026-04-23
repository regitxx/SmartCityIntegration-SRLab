"""Tests for smcity.tools.otp2 — OpenTripPlanner 2 sidecar client.

Respx-mocks the OTP2 HTTP endpoint so CI doesn't need a running Java
sidecar. The goal is to pin the request shape (params OTP2 expects) and
the response parser (OTP2's old-REST plan response → our normalised leg
shape).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from smcity.tools import build_default_registry
from smcity.tools.otp2 import (
    OTP2_BASE_URL,
    OTP2_ROUTER,
    _to_otp_modes,
)
from smcity.tools.registry import ToolContext

_PLAN_URL = f"{OTP2_BASE_URL.rstrip('/')}/routers/{OTP2_ROUTER}/plan"


def test_mode_mapping_lowercase_aliases_to_uppercase() -> None:
    assert _to_otp_modes(["walk", "bus"]) == "WALK,BUS"
    assert _to_otp_modes(["SUBWAY", "walk"]) == "SUBWAY,WALK"
    # Dedup: same mode appearing twice collapses.
    assert _to_otp_modes(["walk", "WALK", "transit"]) == "WALK,TRANSIT"
    # Empty input → sensible default
    assert _to_otp_modes([]) == "TRANSIT,WALK"


def test_tool_registered_in_default_registry() -> None:
    names = set(build_default_registry().names())
    assert "transport.plan_multimodal_journey" in names


@pytest.mark.asyncio
@respx.mock
async def test_otp2_happy_path_parses_itineraries() -> None:
    payload: dict[str, Any] = {
        "plan": {
            "itineraries": [
                {
                    "duration": 2400,
                    "walkDistance": 420,
                    "transitTime": 1800,
                    "startTime": "2026-04-23T10:00:00+08:00",
                    "endTime": "2026-04-23T10:40:00+08:00",
                    "legs": [
                        {
                            "mode": "WALK",
                            "duration": 180,
                            "distance": 220,
                            "from": {"name": "Origin"},
                            "to": {"name": "Central MTR"},
                            "startTime": "2026-04-23T10:00:00+08:00",
                            "endTime": "2026-04-23T10:03:00+08:00",
                        },
                        {
                            "mode": "SUBWAY",
                            "routeShortName": "TCL",
                            "routeLongName": "Tung Chung Line",
                            "agencyName": "MTR",
                            "duration": 1500,
                            "distance": 18000,
                            "from": {"name": "Central"},
                            "to": {"name": "Sha Tin"},
                            "startTime": "2026-04-23T10:05:00+08:00",
                            "endTime": "2026-04-23T10:30:00+08:00",
                        },
                        {
                            "mode": "WALK",
                            "duration": 200,
                            "distance": 200,
                            "from": {"name": "Sha Tin"},
                            "to": {"name": "Destination"},
                        },
                    ],
                }
            ]
        }
    }
    respx.get(_PLAN_URL).mock(return_value=httpx.Response(200, json=payload))

    registry = build_default_registry()
    ctx = ToolContext(session_id="otp-test")
    result = await registry.dispatch(
        "transport.plan_multimodal_journey",
        {
            "origin_lat": 22.2820,
            "origin_lng": 114.1582,
            "destination_lat": 22.3817,
            "destination_lng": 114.1870,
            "modes": ["TRANSIT", "WALK"],
        },
        ctx,
    )
    assert result.status == "ok", result.error
    assert result.result is not None
    its = result.result["itineraries"]
    assert len(its) == 1
    assert its[0]["duration_s"] == 2400
    legs = its[0]["legs"]
    assert [leg["mode"] for leg in legs] == ["WALK", "SUBWAY", "WALK"]
    assert legs[1]["route_short_name"] == "TCL"
    assert legs[1]["agency_name"] == "MTR"


@pytest.mark.asyncio
@respx.mock
async def test_otp2_connection_refused_surfaces_clean_upstream_error() -> None:
    # respx can model a connection refusal via side_effect=httpx.ConnectError.
    respx.get(_PLAN_URL).mock(side_effect=httpx.ConnectError("connection refused"))
    registry = build_default_registry()
    ctx = ToolContext(session_id="otp-test")
    result = await registry.dispatch(
        "transport.plan_multimodal_journey",
        {
            "origin_lat": 22.28,
            "origin_lng": 114.16,
            "destination_lat": 22.38,
            "destination_lng": 114.19,
        },
        ctx,
    )
    assert result.status == "error"
    assert "OTP2 sidecar unreachable" in (result.error or "")
    assert "otp/README.md" in (result.error or "")


@pytest.mark.asyncio
@respx.mock
async def test_otp2_planner_error_payload_surfaces_msg() -> None:
    respx.get(_PLAN_URL).mock(
        return_value=httpx.Response(
            200,
            json={"error": {"msg": "Destination outside bounds."}},
        )
    )
    registry = build_default_registry()
    ctx = ToolContext(session_id="otp-test")
    result = await registry.dispatch(
        "transport.plan_multimodal_journey",
        {
            "origin_lat": 22.28,
            "origin_lng": 114.16,
            "destination_lat": 30.0,  # definitely outside the HK graph
            "destination_lng": 120.0,
        },
        ctx,
    )
    assert result.status == "error"
    assert "Destination outside bounds" in (result.error or "")


@pytest.mark.asyncio
@respx.mock
async def test_otp2_empty_itineraries_surface_helpful_note() -> None:
    respx.get(_PLAN_URL).mock(return_value=httpx.Response(200, json={"plan": {"itineraries": []}}))
    registry = build_default_registry()
    ctx = ToolContext(session_id="otp-test")
    result = await registry.dispatch(
        "transport.plan_multimodal_journey",
        {
            "origin_lat": 22.28,
            "origin_lng": 114.16,
            "destination_lat": 22.38,
            "destination_lng": 114.19,
        },
        ctx,
    )
    assert result.status == "ok"
    assert result.result is not None
    assert result.result["itineraries"] == []
    assert "no itineraries" in (result.result["note"] or "")
