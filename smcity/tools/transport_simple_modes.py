"""Simple non-MTR mode planners — walking + taxi.

Both tools accept origin + destination as explicit lat/lng OR as free-text
names (resolved via the ALS address-lookup helper). Use these when the user
asks about walking or taxi options specifically; `transport.plan_journey`
composes them for "any mode" queries.
"""

from __future__ import annotations

import math
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from smcity.geometry import haversine_m as _haversine_m
from smcity.tools.geo import ALS_URL
from smcity.tools.registry import ToolContext, ToolSpec, ToolUpstreamError

# HK-specific constants (2026).
_WALK_SPEED_MPS = 1.2  # conservative urban pace with stairs + crowds
_DRIVE_SPEED_KPH = 25.0  # conservative HK urban average incl. lights + turns
_TAXI_FLAG_HKD = 27.0  # first 2 km
_TAXI_FLAG_DISTANCE_M = 2000
_TAXI_INCREMENT_HKD = 1.9  # per 200 m after the first 2 km
_TAXI_INCREMENT_DISTANCE_M = 200


# Common HK landmark / institution aliases that ALS doesn't reliably resolve.
# Mapped to the nearest well-known address that ALS DOES geocode, or to an
# MTR station name the catalog will match. Hand-curated; extend as needed.
_LANDMARK_ALIASES: dict[str, str] = {
    "cityu": "City University of Hong Kong, Tat Chee Avenue, Kowloon Tong",
    "polyu": "Hong Kong Polytechnic University, Hung Hom",
    "hku": "HKU",  # MTR station catalog has this directly
    "cuhk": "University",  # MTR station name for CUHK's campus stop
    "hkust": "Clear Water Bay",  # closest landmark
    "ust": "Clear Water Bay",
    "baptist u": "Baptist University, Renfrew Road, Kowloon Tong",
    "hkbu": "Baptist University, Renfrew Road, Kowloon Tong",
    "lingnan u": "Lingnan University, Tuen Mun",
    "ouhk": "OUHK, Homantin",
    "disneyland": "Disneyland Resort",
    "ocean park": "Ocean Park",
    "airport": "Airport",
    "hkia": "Airport",
}


def _normalise_query(query: str) -> str:
    """Apply landmark-alias replacement to free-text location names."""
    if not query:
        return query
    key = query.strip().lower()
    return _LANDMARK_ALIASES.get(key, query)


async def _geocode_via_mtr_catalog(query: str) -> tuple[float, float] | None:
    """Try resolving `query` as an MTR station name; return (lat, lng) if so."""
    # Local imports to avoid cycles at module load time.
    from smcity.tools.transport import resolve_mtr_station
    from smcity.tools.transport_search import MTR_STATION_COORDS

    if not query:
        return None
    station = resolve_mtr_station(query)
    if station is None:
        return None
    coords = MTR_STATION_COORDS.get(station.code)
    if coords is None:
        return None
    return coords[0], coords[1]


async def _geocode_via_als(query: str) -> tuple[float, float] | None:
    """One-shot ALS call returning the best candidate's (lat, lng), or None.

    ALS (www.als.gov.hk) returns its own schema (NOT GeoJSON):
      { "SuggestedAddress": [
          { "Address": { "PremisesAddress": {
              "GeospatialInformation": {
                "Latitude":"22.339...", "Longitude":"114.171...",
                "Northing":"...", "Easting":"..."
              }, "EngPremisesAddress": {...}, ...
          }}}]}
    """
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
    suggestions = data.get("SuggestedAddress") or []
    if not suggestions:
        return None
    premises = (suggestions[0].get("Address") or {}).get("PremisesAddress") or {}
    geo_info = premises.get("GeospatialInformation") or {}
    lat_str = geo_info.get("Latitude")
    lng_str = geo_info.get("Longitude")
    if lat_str and lng_str:
        try:
            return float(lat_str), float(lng_str)
        except (TypeError, ValueError):
            return None
    return None


async def _geocode_one(query: str) -> tuple[float, float] | None:
    """Resolve a free-text place name to (lat, lng).

    Resolution order (cheapest + most reliable first):
      1. Landmark alias dict (CityU, PolyU, HKU, …) — single dict lookup.
      2. MTR station catalog — 105 trilingual stations, fuzzy-matched.
      3. ALS — Lands Department Address Lookup Service. Best for full
         street addresses; sometimes fuzzy-matches building names.

    Returns `None` if nothing resolves with confidence.
    """
    if not query:
        return None
    normalised = _normalise_query(query)
    # 1. MTR station? (cheap, no network, very reliable for stations)
    mtr_coords = await _geocode_via_mtr_catalog(normalised)
    if mtr_coords:
        return mtr_coords
    # 2. ALS over the network for everything else.
    return await _geocode_via_als(normalised)


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
    # MTR-only fields, populated by an internal plan_simple_route call so the
    # LLM never has to make a follow-up call (which it would hallucinate past).
    mtr_origin_station: str | None = None  # e.g. "Hung Hom"
    mtr_destination_station: str | None = None  # e.g. "Kowloon Tong"
    mtr_lines: list[str] | None = None  # e.g. ["East Rail Line"]
    mtr_legs_summary: str | None = None  # one-liner the LLM can paste verbatim


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
        # Inline the actual Dijkstra plan so the LLM never has to make a
        # follow-up call (it hallucinates a fake route when asked to).
        mtr_option = await _inline_mtr_leg(o_lat, o_lng, d_lat, d_lng)
        options.append(mtr_option)

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


async def _inline_mtr_leg(
    o_lat: float, o_lng: float, d_lat: float, d_lng: float
) -> JourneyOption:
    """Run the real Dijkstra MTR planner and embed the result as a JourneyOption.

    Earlier versions returned `mode=mtr, note='Call plan_simple_route'` and
    relied on the LLM to make a follow-up call. In practice gpt-oss-120b
    ignored that and fabricated routes (e.g. "Tsuen Wan line via Yau Ma Tei"
    when the real path was East Rail Line, 2 stops). Solving by inlining
    the real plan so the LLM has actual data in its context.
    """
    # Local import to avoid circular import at module load.
    from smcity.tools.registry import ToolContext
    from smcity.tools.transport_planner import PlanSimpleRouteArgs
    from smcity.tools.transport_planner import _handler as _plan_handler

    dist_km = _haversine_m(o_lat, o_lng, d_lat, d_lng) / 1000
    if dist_km < 0.6:
        return JourneyOption(
            mode="mtr",
            duration_min=None,
            note="MTR possible but walking is likely faster (stations are sparse).",
        )

    args = PlanSimpleRouteArgs(
        origin_lat=o_lat,
        origin_lng=o_lng,
        destination_lat=d_lat,
        destination_lng=d_lng,
    )
    try:
        plan = await _plan_handler(args, ToolContext(session_id="_journey_inline"))
    except Exception:
        return JourneyOption(
            mode="mtr",
            duration_min=None,
            note="MTR routing unavailable for this pair; consider taxi or walk.",
        )

    if not plan.ok or plan.total_duration_min is None:
        return JourneyOption(
            mode="mtr",
            duration_min=None,
            note=plan.reason or "No MTR route found between the nearest stations.",
        )

    # Origin / destination station NAMES (not codes) — what the LLM should quote.
    origin_st = _first_named_station(plan.legs, side="from")
    dest_st = _first_named_station(plan.legs, side="to", reverse=True)
    lines_en: list[str] = []
    for leg in plan.legs:
        if leg.kind == "ride" and leg.line_name_en and leg.line_name_en not in lines_en:
            lines_en.append(leg.line_name_en)

    # Build a verbatim-paste-able one-liner. The prompt tells the LLM to use this.
    if origin_st and dest_st and lines_en:
        lines_str = " + ".join(lines_en)
        summary = (
            f"MTR: walk to {origin_st}, take the {lines_str} to {dest_st}, "
            f"~{plan.total_duration_min} min total."
        )
    else:
        summary = f"~{plan.total_duration_min} min on the MTR."

    return JourneyOption(
        mode="mtr",
        duration_min=plan.total_duration_min,
        note=summary,
        mtr_origin_station=origin_st,
        mtr_destination_station=dest_st,
        mtr_lines=lines_en or None,
        mtr_legs_summary=summary,
    )


def _first_named_station(legs: list[Any], *, side: str, reverse: bool = False) -> str | None:
    """Pick a real transit-station name from the planner's legs list.

    The Dijkstra planner emits a sequence like
        walk('(origin)' → Yau Ma Tei) · board(Yau Ma Tei) · ride(YMT→Central) ·
        alight(Central) · walk(Central → '(destination)')
    when origin/destination are arbitrary coords. The walk legs carry literal
    placeholder strings like "(origin)" / "(destination)" in their from/to
    fields. We must SKIP those and return the first/last station that's
    actually on the MTR network — i.e. the `from`/`to` of a board / ride /
    alight leg, which is always a real station name.
    """
    iterable = reversed(legs) if reverse else legs
    key = f"{side}_name_en"
    for leg in iterable:
        kind = getattr(leg, "kind", None)
        # Skip the walk-to-station and walk-from-station bookends.
        if kind == "walk":
            continue
        name = getattr(leg, key, None)
        if not name:
            continue
        # Defensive: any literal placeholder slipped through? Drop it.
        s = str(name).strip()
        if s.startswith("(") and s.endswith(")"):
            continue
        return s
    return None


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
