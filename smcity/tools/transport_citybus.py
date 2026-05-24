"""Citybus tools — rt.data.gov.hk/v2/transport/citybus.

Citybus doesn't expose a "list-all-stops" endpoint. For v0.1 we ship:
- transport.get_citybus_eta_by_route_stop — ETA for route+stop_id
- transport.get_citybus_route_stops         — stop list for a route+direction

Proximity search for Citybus is deferred to Phase 2 (would require walking
every route's route-stop feed + deduplication).
"""

from __future__ import annotations

from datetime import datetime

import httpx
from pydantic import BaseModel, Field

from smcity.tools.registry import ToolContext, ToolScope, ToolSpec, ToolUpstreamError

CITYBUS_BASE = "https://rt.data.gov.hk/v2/transport/citybus"


def _minutes_until(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    now = datetime.now(dt.tzinfo)
    return max(0, int((dt - now).total_seconds() // 60))


# --- ETA by route + stop -------------------------------------------------


class CitybusEtaArgs(BaseModel):
    route: str = Field(min_length=1, description="Citybus route number, e.g. '1', '8X'.")
    stop_id: str = Field(
        min_length=1,
        description="Citybus 6-digit stop_id (required — Citybus stop IDs are not "
        "searchable by name in this tool).",
    )


class CitybusEtaEntry(BaseModel):
    route: str
    direction: str  # "I" inbound / "O" outbound
    destination_en: str
    destination_tc: str
    destination_sc: str | None = None
    eta_iso: str | None = None
    minutes_until: int | None = None


class CitybusEtaResult(BaseModel):
    route: str
    stop_id: str
    etas: list[CitybusEtaEntry]
    source: str = "rt.data.gov.hk/citybus"


async def _eta_handler(args: CitybusEtaArgs, ctx: ToolContext) -> CitybusEtaResult:
    url = f"{CITYBUS_BASE}/eta/CTB/{args.stop_id}/{args.route}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as h:
            r = await h.get(url)
            r.raise_for_status()
            payload = r.json()
    except httpx.HTTPError as err:
        raise ToolUpstreamError(f"Citybus ETA failed: {err}") from err

    etas: list[CitybusEtaEntry] = []
    for rec in payload.get("data") or []:
        eta_iso = rec.get("eta")
        etas.append(
            CitybusEtaEntry(
                route=str(rec.get("route", args.route)),
                direction=str(rec.get("dir", "")),
                destination_en=str(rec.get("dest_en", "")),
                destination_tc=str(rec.get("dest_tc", "")),
                destination_sc=rec.get("dest_sc") or None,
                eta_iso=eta_iso,
                minutes_until=_minutes_until(eta_iso),
            )
        )
    etas.sort(key=lambda e: (e.minutes_until is None, e.minutes_until or 999))
    return CitybusEtaResult(route=args.route, stop_id=args.stop_id, etas=etas)


CITYBUS_ETA_TOOL: ToolSpec[CitybusEtaArgs, CitybusEtaResult] = ToolSpec(
    name="transport.get_citybus_eta_by_route_stop",
    description_en=(
        "ETA for a Citybus (城巴 CTB) route at a given 6-digit stop_id. Use ONLY "
        "when the user names a Citybus route — do NOT use for KMB / LWB routes "
        "(those have transport.get_kmb_eta_by_route_stop). If the user only has "
        "a name, call geo.address_lookup first and then "
        "transport.get_citybus_route_stops to find the nearest stop_id."
    ),
    args_schema=CitybusEtaArgs,
    result_schema=CitybusEtaResult,
    handler=_eta_handler,
    ttl_seconds=30,
    budget_ms=2000,
    upstream_langs=frozenset({"en", "zh-Hant", "zh-Hans"}),
    upstream="rt.data.gov.hk/citybus",
    scope=ToolScope.SPECIALIZED,
    domain="citybus_only",
)


# --- route-stop listing --------------------------------------------------


class CitybusRouteStopsArgs(BaseModel):
    route: str = Field(min_length=1, description="Citybus route number.")
    direction: str = Field(
        default="inbound",
        pattern=r"^(inbound|outbound)$",
        description="Direction: 'inbound' or 'outbound'.",
    )


class CitybusStopRow(BaseModel):
    sequence: int
    stop_id: str


class CitybusRouteStopsResult(BaseModel):
    route: str
    direction: str
    stops: list[CitybusStopRow]
    source: str = "rt.data.gov.hk/citybus"


async def _route_stops_handler(
    args: CitybusRouteStopsArgs, ctx: ToolContext
) -> CitybusRouteStopsResult:
    url = f"{CITYBUS_BASE}/route-stop/CTB/{args.route}/{args.direction}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as h:
            r = await h.get(url)
            r.raise_for_status()
            payload = r.json()
    except httpx.HTTPError as err:
        raise ToolUpstreamError(f"Citybus route-stop failed: {err}") from err
    stops = [
        CitybusStopRow(sequence=int(rec.get("seq", 0)), stop_id=str(rec.get("stop", "")))
        for rec in (payload.get("data") or [])
    ]
    return CitybusRouteStopsResult(route=args.route, direction=args.direction, stops=stops)


CITYBUS_ROUTE_STOPS_TOOL: ToolSpec[CitybusRouteStopsArgs, CitybusRouteStopsResult] = ToolSpec(
    name="transport.get_citybus_route_stops",
    description_en=(
        "Ordered list of Citybus stops for a route + direction. Returns only "
        "stop_ids; call transport.get_citybus_eta_by_route_stop with each to get "
        "names + ETAs."
    ),
    args_schema=CitybusRouteStopsArgs,
    result_schema=CitybusRouteStopsResult,
    handler=_route_stops_handler,
    ttl_seconds=60 * 60,
    budget_ms=2000,
    upstream_langs=frozenset({"en", "zh-Hant", "zh-Hans"}),
    upstream="rt.data.gov.hk/citybus",
    scope=ToolScope.SPECIALIZED,
    domain="citybus_only",
)
