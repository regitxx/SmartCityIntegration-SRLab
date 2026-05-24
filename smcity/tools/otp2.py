# ruff: noqa: RUF001
"""OpenTripPlanner 2 sidecar client — multimodal journey planning.

smcity ships with a hand-rolled Dijkstra planner over the MTR topology
(`transport.plan_simple_route`). That's great for MTR-dominated queries
but cannot answer true multimodal questions: "KMB 1A then walk then
MTR" or "minibus 69 then ferry to Cheung Chau." OTP2 fills the gap.

This module is a THIN async client over a locally running OTP2 HTTP
sidecar (see `otp/README.md` for setup). The tool spec exposes one
entry point — `transport.plan_multimodal_journey(origin, destination,
modes, date, time, arrive_by)` — and maps OTP2's `/otp/routers/default/plan`
response into the same normalised leg shape our simple planner already
uses, so the LLM's mental model is consistent.

Runtime dependency: a Java 21 OTP2 instance reachable at `OTP2_BASE_URL`
(default `http://127.0.0.1:8080/otp`). If unreachable, the tool returns
a clean `ToolUpstreamError` with a hint pointing at `otp/README.md`. The
agent falls back to `transport.plan_simple_route` in that case.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import BaseModel, Field

from smcity.tools.registry import ToolContext, ToolScope, ToolSpec, ToolUpstreamError

OTP2_BASE_URL = os.environ.get("OTP2_BASE_URL", "http://127.0.0.1:8080/otp")
OTP2_ROUTER = os.environ.get("OTP2_ROUTER", "default")
OTP2_TIMEOUT_S = float(os.environ.get("OTP2_TIMEOUT_S", "10"))

# OTP2's GraphQL transport mode enum — the agent-facing set. We accept a
# subset we know works on a HK GTFS graph; omit rail-bike / scooter etc.
_OTP_MODE_MAP: dict[str, str] = {
    "walk": "WALK",
    "bus": "BUS",
    "rail": "RAIL",
    "tram": "TRAM",
    "subway": "SUBWAY",
    "ferry": "FERRY",
    "transit": "TRANSIT",  # any public transit
}


class PlanMultimodalArgs(BaseModel):
    origin_lat: float = Field(ge=-90, le=90)
    origin_lng: float = Field(ge=-180, le=180)
    destination_lat: float = Field(ge=-90, le=90)
    destination_lng: float = Field(ge=-180, le=180)
    modes: list[str] = Field(
        default_factory=lambda: ["TRANSIT", "WALK"],
        description=(
            "Transport modes to allow. Use the OTP2 uppercase names "
            "('TRANSIT', 'BUS', 'RAIL', 'SUBWAY', 'FERRY', 'TRAM', 'WALK') "
            "or the lowercase aliases ('bus', 'walk', 'ferry', ...)."
        ),
    )
    date: str | None = Field(
        default=None,
        description="Departure date in YYYY-MM-DD (OTP2 format). Defaults to today.",
    )
    time: str | None = Field(
        default=None,
        description="Departure/arrival time in HH:MM (24h, HK local). Defaults to now.",
    )
    arrive_by: bool = Field(default=False, description="True = plan backwards from arrival time.")
    num_itineraries: int = Field(default=3, ge=1, le=5)


class PlanLeg(BaseModel):
    mode: str
    route_short_name: str | None = None  # e.g. 'Tuen Ma Line', '1A', 'S5'
    route_long_name: str | None = None
    agency_name: str | None = None
    from_stop: str | None = None
    to_stop: str | None = None
    start_time: str | None = None  # ISO-8601 with offset
    end_time: str | None = None
    duration_s: int | None = None
    distance_m: int | None = None


class PlanItinerary(BaseModel):
    duration_s: int
    walk_distance_m: int
    transit_duration_s: int
    start_time: str | None = None
    end_time: str | None = None
    legs: list[PlanLeg]


class PlanMultimodalResult(BaseModel):
    itineraries: list[PlanItinerary]
    router: str = OTP2_ROUTER
    source: str = "otp2.sidecar"
    note: str | None = None


def _to_otp_modes(raw: list[str]) -> str:
    normalised: list[str] = []
    seen: set[str] = set()
    for m in raw:
        key = m.strip()
        upper = key.upper()
        resolved = _OTP_MODE_MAP.get(key.lower(), upper)
        if resolved not in seen:
            seen.add(resolved)
            normalised.append(resolved)
    return ",".join(normalised) if normalised else "TRANSIT,WALK"


def _now_hk_date() -> str:
    # OTP2 takes the graph's local-time date. HK is UTC+8, no DST.
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _now_hk_time() -> str:
    from datetime import timedelta

    return (datetime.now(UTC) + timedelta(hours=8)).strftime("%H:%M")


def _parse_leg(raw: dict[str, Any]) -> PlanLeg:
    return PlanLeg(
        mode=str(raw.get("mode") or "UNKNOWN"),
        route_short_name=raw.get("routeShortName"),
        route_long_name=raw.get("routeLongName"),
        agency_name=raw.get("agencyName"),
        from_stop=((raw.get("from") or {}).get("name")),
        to_stop=((raw.get("to") or {}).get("name")),
        start_time=raw.get("startTime") if isinstance(raw.get("startTime"), str) else None,
        end_time=raw.get("endTime") if isinstance(raw.get("endTime"), str) else None,
        duration_s=int(raw["duration"]) if isinstance(raw.get("duration"), (int, float)) else None,
        distance_m=int(raw["distance"]) if isinstance(raw.get("distance"), (int, float)) else None,
    )


def _parse_itinerary(raw: dict[str, Any]) -> PlanItinerary:
    legs = [_parse_leg(leg) for leg in (raw.get("legs") or [])]
    return PlanItinerary(
        duration_s=int(raw.get("duration") or 0),
        walk_distance_m=int(raw.get("walkDistance") or 0),
        transit_duration_s=int(raw.get("transitTime") or 0),
        start_time=raw.get("startTime") if isinstance(raw.get("startTime"), str) else None,
        end_time=raw.get("endTime") if isinstance(raw.get("endTime"), str) else None,
        legs=legs,
    )


async def _plan_handler(args: PlanMultimodalArgs, ctx: ToolContext) -> PlanMultimodalResult:
    params: dict[str, str] = {
        "fromPlace": f"{args.origin_lat},{args.origin_lng}",
        "toPlace": f"{args.destination_lat},{args.destination_lng}",
        "mode": _to_otp_modes(args.modes),
        "date": args.date or _now_hk_date(),
        "time": args.time or _now_hk_time(),
        "arriveBy": "true" if args.arrive_by else "false",
        "numItineraries": str(args.num_itineraries),
    }
    url = f"{OTP2_BASE_URL.rstrip('/')}/routers/{OTP2_ROUTER}/plan"
    try:
        async with httpx.AsyncClient(timeout=OTP2_TIMEOUT_S) as http:
            resp = await http.get(url, params=params)
            resp.raise_for_status()
            payload = resp.json()
    except httpx.ConnectError as err:
        raise ToolUpstreamError(
            f"OTP2 sidecar unreachable at {url} — start it per otp/README.md "
            f"or fall back to transport.plan_simple_route. ({err})"
        ) from err
    except httpx.HTTPError as err:
        raise ToolUpstreamError(f"OTP2 plan failed: {err}") from err
    except ValueError as err:
        raise ToolUpstreamError(f"OTP2 non-JSON: {err}") from err

    # OTP2's old-REST shape is {"plan": {"itineraries": [...]}, "error": ...}.
    if isinstance(payload.get("error"), dict):
        msg = payload["error"].get("msg") or payload["error"].get("message") or "unknown error"
        raise ToolUpstreamError(f"OTP2 planner error: {msg}")
    plan_block = payload.get("plan") or {}
    raw_its = plan_block.get("itineraries") or []
    itineraries = [_parse_itinerary(it) for it in raw_its]
    note = None
    if not itineraries:
        note = "no itineraries returned — origin/destination may be outside the graph extent"
    return PlanMultimodalResult(itineraries=itineraries, router=OTP2_ROUTER, note=note)


PLAN_MULTIMODAL_JOURNEY_TOOL: ToolSpec[PlanMultimodalArgs, PlanMultimodalResult] = ToolSpec(
    name="transport.plan_multimodal_journey",
    description_en=(
        "Plan a true multimodal HK journey (walk + bus + MTR + minibus + ferry) "
        "using an OpenTripPlanner 2 sidecar built on HK GTFS feeds. Provide "
        "origin + destination as lat/lng. OPTIONAL modes filter accepts "
        "'TRANSIT'/'WALK'/'BUS'/'RAIL'/'SUBWAY'/'FERRY' or lowercase aliases. "
        "Returns 1–5 itineraries, each with timed legs. Use ONLY when the user "
        "asks for a full multi-leg journey across multiple modes; "
        "transport.plan_journey is cheaper and sufficient for the common "
        "walk-or-MTR case. If the sidecar is offline the call raises an "
        "upstream error — fall back to transport.plan_simple_route for "
        "MTR-only requests."
    ),
    args_schema=PlanMultimodalArgs,
    result_schema=PlanMultimodalResult,
    handler=_plan_handler,
    ttl_seconds=60,  # transit data changes with timetables; short TTL
    budget_ms=5000,
    upstream_langs=frozenset({"en", "zh-Hant"}),
    upstream="otp2 (local sidecar)",
    scope=ToolScope.SPECIALIZED,
    domain="multimodal_journey",
)
