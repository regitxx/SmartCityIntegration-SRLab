"""Simple non-MTR mode planners — walking + taxi.

Both tools accept origin + destination as explicit lat/lng OR as free-text
names (resolved via the ALS address-lookup helper). Use these when the user
asks about walking or taxi options specifically; `transport.plan_journey`
composes them for "any mode" queries.
"""

from __future__ import annotations

import math
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from smcity.tools.geo import ALS_URL
from smcity.tools.registry import ToolContext, ToolSpec, ToolUpstreamError

# HK-specific constants (2026).
_WALK_SPEED_MPS = 1.2  # conservative urban pace with stairs + crowds
_DRIVE_SPEED_KPH = 25.0  # conservative HK urban average incl. lights + turns
_TAXI_FLAG_HKD = 27.0  # first 2 km
_TAXI_FLAG_DISTANCE_M = 2000
_TAXI_INCREMENT_HKD = 1.9  # per 200 m after the first 2 km
_TAXI_INCREMENT_DISTANCE_M = 200


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


async def _geocode_one(query: str) -> tuple[float, float] | None:
    """One-shot ALS call returning the best candidate's (lat, lng), or None."""
    if not query:
        return None
    try:
        async with httpx.AsyncClient(timeout=4.0) as h:
            r = await h.get(
                ALS_URL,
                headers={"Accept": "application/json", "Accept-Language": "en,zh-Hant"},
                params={"q": query, "n": 1},
            )
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError:
        return None
    feats = data.get("features") or []
    if not feats:
        return None
    props = feats[0].get("properties") or {}
    geom = feats[0].get("geometry") or {}
    coords = geom.get("coordinates") or []
    if len(coords) == 2:
        return float(coords[1]), float(coords[0])  # (lat, lng)
    if "lat" in props and "lng" in props:
        return float(props["lat"]), float(props["lng"])
    return None


class _EndpointArgs(BaseModel):
    origin: str | None = Field(
        default=None,
        description="Free-text origin name (e.g. 'Mong Kok', 'Choi Hung Estate').",
    )
    origin_lat: float | None = Field(default=None, ge=-90, le=90)
    origin_lng: float | None = Field(default=None, ge=-180, le=180)
    destination: str | None = Field(
        default=None,
        description="Free-text destination name (e.g. 'Holy Cross Church', 'Sha Tin').",
    )
    destination_lat: float | None = Field(default=None, ge=-90, le=90)
    destination_lng: float | None = Field(default=None, ge=-180, le=180)


async def _resolve_pair(
    args: _EndpointArgs,
) -> tuple[tuple[float, float], tuple[float, float], dict[str, str | None]]:
    """Resolve origin + destination to (lat, lng) pairs via ALS when needed."""
    origin_pair: tuple[float, float] | None = None
    dest_pair: tuple[float, float] | None = None
    resolved: dict[str, str | None] = {"origin_resolved": None, "destination_resolved": None}

    if args.origin_lat is not None and args.origin_lng is not None:
        origin_pair = (args.origin_lat, args.origin_lng)
    elif args.origin:
        origin_pair = await _geocode_one(args.origin)
        resolved["origin_resolved"] = args.origin

    if args.destination_lat is not None and args.destination_lng is not None:
        dest_pair = (args.destination_lat, args.destination_lng)
    elif args.destination:
        dest_pair = await _geocode_one(args.destination)
        resolved["destination_resolved"] = args.destination

    if origin_pair is None:
        raise ToolUpstreamError(
            "Origin could not be resolved — provide origin_lat/lng or a clearer name."
        )
    if dest_pair is None:
        raise ToolUpstreamError(
            "Destination could not be resolved — provide destination_lat/lng or a clearer name."
        )
    return origin_pair, dest_pair, resolved


# --- plan_walking_route --------------------------------------------------


class PlanWalkingArgs(_EndpointArgs):
    pass


class PlanWalkingResult(BaseModel):
    ok: bool
    distance_m: int
    duration_min: int
    speed_mps: float = _WALK_SPEED_MPS
    origin_lat: float
    origin_lng: float
    destination_lat: float
    destination_lng: float
    origin_resolved: str | None = None
    destination_resolved: str | None = None
    advice: str | None = None
    source: str = "smcity.planner (walk)"


async def _walking_handler(args: PlanWalkingArgs, ctx: ToolContext) -> PlanWalkingResult:
    (o_lat, o_lng), (d_lat, d_lng), resolved = await _resolve_pair(args)
    dist_m = _haversine_m(o_lat, o_lng, d_lat, d_lng)
    minutes = max(1, round(dist_m / (_WALK_SPEED_MPS * 60)))
    advice: str | None = None
    if dist_m > 5000:
        advice = "That's over 5 km — consider MTR / bus / taxi instead."
    elif dist_m > 2500:
        advice = "Long walk — check weather and bring water."
    return PlanWalkingResult(
        ok=True,
        distance_m=round(dist_m),
        duration_min=minutes,
        origin_lat=o_lat,
        origin_lng=o_lng,
        destination_lat=d_lat,
        destination_lng=d_lng,
        origin_resolved=resolved["origin_resolved"],
        destination_resolved=resolved["destination_resolved"],
        advice=advice,
    )


PLAN_WALKING_TOOL: ToolSpec[PlanWalkingArgs, PlanWalkingResult] = ToolSpec(
    name="transport.plan_walking_route",
    description_en=(
        "Estimate walking distance + duration between two points in Hong Kong. "
        "Accepts origin + destination either as free-text names (geocoded via "
        "ALS) or as explicit lat/lng pairs. Assumes 1.2 m/s HK-urban walking "
        "pace. Use for 'how far / long is it to walk from X to Y?' queries — "
        "do NOT use transport.plan_simple_route (that's MTR-only)."
    ),
    args_schema=PlanWalkingArgs,
    result_schema=PlanWalkingResult,
    handler=_walking_handler,
    ttl_seconds=60 * 60,
    budget_ms=1500,
    upstream_langs=frozenset({"en", "zh-Hant"}),
    upstream="smcity.planner",
)


# --- plan_taxi_estimate --------------------------------------------------


def _taxi_fare_hkd(distance_m: float) -> tuple[int, int]:
    """Return (low, high) HKD fare estimate for a HK urban taxi trip.

    Formula (2026 urban rates):
      - Flag-down: HK$27 for the first 2 km
      - HK$1.90 for every additional 200 m
    We return a range (base, base * 1.25) to account for traffic + tunnel
    tolls + off-peak night surcharges that don't fit a deterministic formula.
    """
    if distance_m <= _TAXI_FLAG_DISTANCE_M:
        base = _TAXI_FLAG_HKD
    else:
        extra = distance_m - _TAXI_FLAG_DISTANCE_M
        increments = math.ceil(extra / _TAXI_INCREMENT_DISTANCE_M)
        base = _TAXI_FLAG_HKD + increments * _TAXI_INCREMENT_HKD
    return round(base), round(base * 1.25)


class PlanTaxiArgs(_EndpointArgs):
    pass


class PlanTaxiResult(BaseModel):
    ok: bool
    distance_m: int
    duration_min: int
    fare_hkd_low: int
    fare_hkd_high: int
    fare_explanation: str
    origin_lat: float
    origin_lng: float
    destination_lat: float
    destination_lng: float
    origin_resolved: str | None = None
    destination_resolved: str | None = None
    source: str = "smcity.planner (taxi)"


async def _taxi_handler(args: PlanTaxiArgs, ctx: ToolContext) -> PlanTaxiResult:
    (o_lat, o_lng), (d_lat, d_lng), resolved = await _resolve_pair(args)
    dist_m = _haversine_m(o_lat, o_lng, d_lat, d_lng)
    # Taxi follows roads, not straight line — inflate the haversine by ~1.3 for
    # an honest street-distance proxy.
    road_m = dist_m * 1.3
    minutes = max(3, round(road_m / (_DRIVE_SPEED_KPH * 1000 / 60)))
    low, high = _taxi_fare_hkd(road_m)
    return PlanTaxiResult(
        ok=True,
        distance_m=round(road_m),
        duration_min=minutes,
        fare_hkd_low=low,
        fare_hkd_high=high,
        fare_explanation=(
            "Urban taxi tariff (2026): HK$27 flag-down for first 2 km, "
            "HK$1.90 per 200 m thereafter. High end includes traffic + tolls."
        ),
        origin_lat=o_lat,
        origin_lng=o_lng,
        destination_lat=d_lat,
        destination_lng=d_lng,
        origin_resolved=resolved["origin_resolved"],
        destination_resolved=resolved["destination_resolved"],
    )


PLAN_TAXI_TOOL: ToolSpec[PlanTaxiArgs, PlanTaxiResult] = ToolSpec(
    name="transport.plan_taxi_estimate",
    description_en=(
        "Estimate HK urban-taxi fare and duration between two points. Accepts "
        "free-text names (geocoded via ALS) or lat/lng pairs. Returns a fare "
        "range in HKD using the official 2026 urban tariff, plus a rough "
        "duration at 25 km/h average. Fare range accounts for tunnel tolls + "
        "night/peak surcharge."
    ),
    args_schema=PlanTaxiArgs,
    result_schema=PlanTaxiResult,
    handler=_taxi_handler,
    ttl_seconds=60 * 60,
    budget_ms=1500,
    upstream_langs=frozenset({"en", "zh-Hant"}),
    upstream="smcity.planner",
)


# --- unified plan_journey ------------------------------------------------


_JourneyMode = Literal["walk", "mtr", "taxi"]


def _default_modes() -> list[_JourneyMode]:
    return ["walk", "mtr", "taxi"]


class PlanJourneyArgs(_EndpointArgs):
    modes: list[_JourneyMode] = Field(
        default_factory=_default_modes,
        description="Which modes to evaluate. Defaults to all three.",
    )


class JourneyOption(BaseModel):
    mode: Literal["walk", "mtr", "taxi"]
    distance_m: int | None = None
    duration_min: int | None = None
    fare_hkd_range: tuple[int, int] | None = None
    note: str | None = None


class PlanJourneyResult(BaseModel):
    origin_lat: float
    origin_lng: float
    destination_lat: float
    destination_lng: float
    origin_resolved: str | None = None
    destination_resolved: str | None = None
    options: list[JourneyOption]
    recommendation: str
    source: str = "smcity.planner (journey)"


async def _journey_handler(args: PlanJourneyArgs, ctx: ToolContext) -> PlanJourneyResult:
    (o_lat, o_lng), (d_lat, d_lng), resolved = await _resolve_pair(args)
    dist_m = _haversine_m(o_lat, o_lng, d_lat, d_lng)
    road_m = dist_m * 1.3
    options: list[JourneyOption] = []
    modes = set(args.modes)

    if "walk" in modes:
        minutes = max(1, round(dist_m / (_WALK_SPEED_MPS * 60)))
        options.append(
            JourneyOption(
                mode="walk",
                distance_m=round(dist_m),
                duration_min=minutes,
                note=_walk_note(dist_m),
            )
        )

    if "mtr" in modes:
        # Pick a coarse MTR estimate from nearest-station + straight-line; the
        # full Dijkstra path is available via transport.plan_simple_route.
        mtr_note = _mtr_summary_note(o_lat, o_lng, d_lat, d_lng)
        options.append(
            JourneyOption(
                mode="mtr",
                duration_min=None,  # delegated — LLM should call plan_simple_route
                note=mtr_note,
            )
        )

    if "taxi" in modes:
        minutes = max(3, round(road_m / (_DRIVE_SPEED_KPH * 1000 / 60)))
        low, high = _taxi_fare_hkd(road_m)
        options.append(
            JourneyOption(
                mode="taxi",
                distance_m=round(road_m),
                duration_min=minutes,
                fare_hkd_range=(low, high),
                note="Urban HK tariff (2026); range covers tolls + off-peak.",
            )
        )

    return PlanJourneyResult(
        origin_lat=o_lat,
        origin_lng=o_lng,
        destination_lat=d_lat,
        destination_lng=d_lng,
        origin_resolved=resolved["origin_resolved"],
        destination_resolved=resolved["destination_resolved"],
        options=options,
        recommendation=_recommendation(dist_m),
    )


def _walk_note(dist_m: float) -> str | None:
    if dist_m < 800:
        return "Short walk — faster than queueing for any other mode."
    if dist_m < 2000:
        return "Comfortable walk for most people."
    if dist_m < 5000:
        return "Long walk; check weather and bring water."
    return "Too far for a practical walk — consider MTR / bus / taxi."


def _recommendation(dist_m: float) -> str:
    if dist_m < 800:
        return "walk"
    if dist_m < 5000:
        return "mtr"
    return "taxi"


def _mtr_summary_note(o_lat: float, o_lng: float, d_lat: float, d_lng: float) -> str:
    # Straight-line distance to a rough MTR estimate (real routing is
    # transport.plan_simple_route). Just a hint for the LLM to know it's an
    # option.
    dist_km = _haversine_m(o_lat, o_lng, d_lat, d_lng) / 1000
    if dist_km < 0.6:
        return "MTR possible but walking is likely faster (stations are sparse)."
    return (
        "Call transport.plan_simple_route for an accurate MTR leg-by-leg plan "
        "between the nearest stations."
    )


PLAN_JOURNEY_TOOL: ToolSpec[PlanJourneyArgs, PlanJourneyResult] = ToolSpec(
    name="transport.plan_journey",
    description_en=(
        "Compare walk + MTR + taxi options for a trip between two HK points in "
        "ONE call. Use this as the DEFAULT when the user asks 'how do I get "
        "from X to Y?' without specifying a mode — it returns a short "
        "side-by-side of durations + taxi fare range + a recommendation, so "
        "you don't need to ask mode first. Accepts free-text names or lat/lng."
    ),
    args_schema=PlanJourneyArgs,
    result_schema=PlanJourneyResult,
    handler=_journey_handler,
    ttl_seconds=60 * 60,
    budget_ms=2500,
    upstream_langs=frozenset({"en", "zh-Hant"}),
    upstream="smcity.planner",
)


__all__ = [
    "PLAN_JOURNEY_TOOL",
    "PLAN_TAXI_TOOL",
    "PLAN_WALKING_TOOL",
]
