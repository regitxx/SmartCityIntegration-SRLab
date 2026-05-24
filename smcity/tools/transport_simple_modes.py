"""Walking + multi-mode journey planning.

Two tools live here:

  * `transport.plan_walking_route` — haversine + HK-urban pace.
  * `transport.plan_journey`       — bundles walk + MTR into one shot so the
                                     LLM gets every viable option without a
                                     follow-up tool call.

Both share the same free-text geocoder, designed to hit real public APIs
first and accept whatever Hong Kong place name the user types — no
hardcoded per-landmark dict.

Taxi was removed in v0.4.12 — the brand promise of this project is "real
HK government data", and a distance * tariff calculation isn't that.
Walking + MTR + (future) KMB/Citybus/GMB are the legitimate path.
"""

from __future__ import annotations

from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from smcity.geometry import haversine_m as _haversine_m
from smcity.tools.geo import ALS_URL
from smcity.tools.registry import ToolContext, ToolScope, ToolSpec, ToolUpstreamError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WALK_SPEED_MPS = 1.2  # conservative urban pace with stairs + crowds

# If origin and destination geocode within this many metres of each other we
# refuse the plan rather than answer "1 min walk" — almost always a sign that
# the geocoder collided on a common substring (the v0.4.11 PolyU = CityU bug).
_COLLISION_THRESHOLD_M = 100.0

# OSM Nominatim — primary free-text geocoder. The `viewbox` is the Hong
# Kong bounding box (left, top, right, bottom) and `bounded=1` clamps
# results to it; without this, "理工大學" matches Harbin University in
# mainland China and "Stanley" matches Stanley, Idaho.
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_HK_VIEWBOX = "113.83,22.56,114.43,22.15"

# Nominatim's usage policy requires a meaningful User-Agent identifying
# the application. https://operations.osmfoundation.org/policies/nominatim/
_NOMINATIM_USER_AGENT = "smcity-agent/0.4.13 (Lab of Social Robotics; HK smart-city assistant)"


# ---------------------------------------------------------------------------
# Geocoder
# ---------------------------------------------------------------------------
#
# Versions up to v0.4.12 carried a hand-maintained dict mapping ~84 specific
# strings to coordinates for seven universities + Disneyland. That was a
# patch list — it covered only what we'd tested, and would have failed for
# the next restaurant, mall, hospital, park, beach, hotel, school, or
# street a real user asked about. Replaced in v0.4.13 with OSM Nominatim
# (viewbox-bounded to HK), which has comprehensive Hong Kong coverage in
# EN / 繁 / 简 from community-edited OSM data and handles the long-tail
# automatically — no per-landmark Python changes ever needed. ALS remains
# as a fallback for street-level addresses Nominatim may miss.


def _exact_mtr_station_match(query: str) -> tuple[float, float] | None:
    """Resolve `query` to MTR station coords by EXACT name match.

    Matches case-insensitively against every language variant of every
    station in the catalog. Returns ``None`` if no language variant matches
    exactly — we deliberately do NOT fuzzy-match here, because the previous
    `fuzz.WRatio`-based path produced false positives like ``"Polytechnic
    University Hong Kong"`` → MTR "University" station (CUHK), where any
    single-word station name (Central / Airport / Kowloon / HKU / …)
    appearing as a substring scored ≥78. The downstream tiers (Nominatim
    + ALS) handle the rest correctly; this short-circuit is only for
    queries that ARE a real station name (``"Kowloon Tong"`` / ``"九龍塘"``).
    """
    if not query:
        return None
    norm = query.strip().casefold()
    if not norm:
        return None
    # Local imports avoid an import cycle at module load.
    from smcity.tools.transport import _load_stations
    from smcity.tools.transport_search import MTR_STATION_COORDS

    for station in _load_stations():
        for name in station.names.values():
            if name and name.strip().casefold() == norm:
                coords = MTR_STATION_COORDS.get(station.code)
                if coords:
                    return coords[0], coords[1]
    return None


async def _geocode_via_nominatim(query: str) -> tuple[float, float] | None:
    """OSM Nominatim, bounded to the HK viewbox, picking the
    highest-importance candidate from the top 5 results.

    Why Nominatim (rather than ALS-only): OSM has comprehensive
    Hong Kong coverage including landmarks, restaurants, malls, temples,
    parks, suburbs, MTR exits, schools, hospitals — anything tagged with
    a `name` / `name:en` / `name:zh-Hant` / `name:zh-Hans` in OSM. The
    `viewbox` + `bounded=1` combo clamps results to HK so we never get
    cross-border collisions (e.g. "理工大學" → Harbin Institute of
    Technology, "Stanley" → Stanley, Idaho).

    Why pick by `importance` rather than first result: viewbox-bounded
    queries don't guarantee an importance-sorted response, and the most
    prominent place is almost always what the user means (`Times Square`
    the Causeway Bay landmark over an obscure street with `times` in its
    name).
    """
    if not query:
        return None
    params = {
        "q": query,
        "format": "json",
        "limit": "5",
        "viewbox": _HK_VIEWBOX,
        "bounded": "1",
        "accept-language": "en,zh-Hant,zh-Hans",
    }
    headers = {"User-Agent": _NOMINATIM_USER_AGENT}
    try:
        async with httpx.AsyncClient(timeout=5.0) as h:
            r = await h.get(NOMINATIM_URL, params=params, headers=headers)
            r.raise_for_status()
            results = r.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(results, list) or not results:
        return None
    try:
        best = max(results, key=lambda e: float(e.get("importance", 0) or 0))
        return float(best["lat"]), float(best["lon"])
    except (KeyError, ValueError, TypeError):
        return None


async def _geocode_via_als(query: str) -> tuple[float, float] | None:
    """ALS (Address Lookup Service, www.als.gov.hk) — Lands Department's
    official HK address geocoder. Best for street-level addresses /
    estate-and-block / building numbers that Nominatim may not have.

    Schema (verified 2026-05-19 against the live endpoint)::

        { "SuggestedAddress": [
            { "Address": { "PremisesAddress": {
                "GeospatialInformation": { "Latitude": "...", "Longitude": "..." },
                "EngPremisesAddress": {...},
                "ChiPremisesAddress": {...},
            }}}]}
    """
    if not query:
        return None
    try:
        async with httpx.AsyncClient(timeout=4.0) as h:
            r = await h.get(
                ALS_URL,
                headers={
                    "Accept": "application/json",
                    "Accept-Language": "zh-Hant,zh-Hans,en",
                },
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
    if not (lat_str and lng_str):
        return None
    try:
        return float(lat_str), float(lng_str)
    except (TypeError, ValueError):
        return None


async def _geocode_one(query: str) -> tuple[float, float] | None:
    """Resolve a free-text place name to (lat, lng).

    Resolution order — each tier is hit only if the previous returned
    ``None``:

      1. **Exact MTR station match** — case-insensitive, multilingual,
         no fuzz. Routes ``"Kowloon Tong"`` / ``"九龍塘"`` straight to
         station coords for high-precision routing. Cheapest tier
         (in-memory dict lookup), deterministic.
      2. **OSM Nominatim** with HK viewbox — comprehensive landmarks,
         POIs, restaurants, malls, suburbs, temples, parks, etc. in
         EN / 繁 / 简. Picks the highest-importance candidate.
      3. **ALS** — Lands Department's official address service.
         Catches street-level addresses (`11 Yuk Choi Road`,
         `Block 5 Whampoa Estate`) Nominatim may not have.

    Returns ``None`` if every tier declined.
    """
    if not query:
        return None
    mtr = _exact_mtr_station_match(query)
    if mtr:
        return mtr
    nominatim = await _geocode_via_nominatim(query)
    if nominatim:
        return nominatim
    return await _geocode_via_als(query)


# ---------------------------------------------------------------------------
# Shared arg + resolve helpers
# ---------------------------------------------------------------------------


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
    """Resolve origin + destination, then collision-guard.

    Raises ``ToolUpstreamError`` if either side fails to resolve, or if both
    sides collapse to the same point (geocoder collision — see
    ``_COLLISION_THRESHOLD_M``).
    """
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
            f"Origin {args.origin!r} could not be resolved — provide origin_lat/lng "
            "or a clearer name (e.g. an MTR station, an estate name, or a street address)."
        )
    if dest_pair is None:
        raise ToolUpstreamError(
            f"Destination {args.destination!r} could not be resolved — provide "
            "destination_lat/lng or a clearer name."
        )

    # Collision guard — geocoder produced the same point for both endpoints.
    if args.origin and args.destination:
        sep_m = _haversine_m(origin_pair[0], origin_pair[1], dest_pair[0], dest_pair[1])
        if sep_m < _COLLISION_THRESHOLD_M:
            raise ToolUpstreamError(
                f"Origin {args.origin!r} and destination {args.destination!r} "
                f"both resolved to nearly the same coordinates "
                f"({sep_m:.0f} m apart) — please clarify with more specific "
                "names (e.g. the campus name plus district, or an MTR station name)."
            )

    return origin_pair, dest_pair, resolved


# ---------------------------------------------------------------------------
# plan_walking_route
# ---------------------------------------------------------------------------


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
        advice = "That's over 5 km — consider MTR or bus instead."
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
        "OSM Nominatim with an ALS fallback) or as explicit lat/lng pairs. "
        "Assumes 1.2 m/s HK-urban walking pace. Use for 'how far / long is it "
        "to walk from X to Y?' queries — do NOT use transport.plan_simple_route "
        "(that's MTR-only)."
    ),
    args_schema=PlanWalkingArgs,
    result_schema=PlanWalkingResult,
    handler=_walking_handler,
    ttl_seconds=60 * 60,
    budget_ms=2500,
    upstream_langs=frozenset({"en", "zh-Hant"}),
    upstream="smcity.planner",
    scope=ToolScope.SPECIALIZED,
    domain="walking_only",
)


# ---------------------------------------------------------------------------
# plan_journey (walk + MTR)
# ---------------------------------------------------------------------------


_JourneyMode = Literal["walk", "mtr"]


def _default_modes() -> list[_JourneyMode]:
    return ["walk", "mtr"]


class PlanJourneyArgs(_EndpointArgs):
    modes: list[_JourneyMode] = Field(
        default_factory=_default_modes,
        description="Which modes to evaluate. Defaults to walk + MTR.",
    )


class JourneyOption(BaseModel):
    mode: _JourneyMode
    distance_m: int | None = None
    duration_min: int | None = None
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
        options.append(await _inline_mtr_leg(o_lat, o_lng, d_lat, d_lng))

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
    return "Too far for a practical walk — take the MTR or a bus."


def _recommendation(dist_m: float) -> str:
    if dist_m < 800:
        return "walk"
    return "mtr"


async def _inline_mtr_leg(o_lat: float, o_lng: float, d_lat: float, d_lng: float) -> JourneyOption:
    """Run the real Dijkstra MTR planner and embed the result as a JourneyOption.

    Earlier versions returned `mode=mtr, note='Call plan_simple_route'` and
    relied on the LLM to make a follow-up call. In practice gpt-oss-120b
    ignored that and fabricated routes (e.g. "Tsuen Wan line via Yau Ma Tei"
    when the real path was East Rail Line, 2 stops). Solving by inlining
    the real plan so the LLM has actual data in its context.
    """
    # Local import to avoid circular import at module load.
    from smcity.tools.registry import ToolContext as _Ctx
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
        plan = await _plan_handler(args, _Ctx(session_id="_journey_inline"))
    except Exception:
        return JourneyOption(
            mode="mtr",
            duration_min=None,
            note="MTR routing unavailable for this pair; consider a bus or walking.",
        )

    if not plan.ok or plan.total_duration_min is None:
        return JourneyOption(
            mode="mtr",
            duration_min=None,
            note=plan.reason or "No MTR route found between the nearest stations.",
        )

    origin_st = _first_named_station(plan.legs, side="from")
    dest_st = _first_named_station(plan.legs, side="to", reverse=True)
    lines_en: list[str] = []
    for leg in plan.legs:
        if leg.kind == "ride" and leg.line_name_en and leg.line_name_en not in lines_en:
            lines_en.append(leg.line_name_en)

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
        if kind == "walk":
            continue
        name = getattr(leg, key, None)
        if not name:
            continue
        s = str(name).strip()
        if s.startswith("(") and s.endswith(")"):
            continue
        return s
    return None


PLAN_JOURNEY_TOOL: ToolSpec[PlanJourneyArgs, PlanJourneyResult] = ToolSpec(
    name="transport.plan_journey",
    description_en=(
        "Compare walk + MTR for a trip between two HK points in ONE call. "
        "Use this as the DEFAULT when the user asks 'how do I get from X to Y?' "
        "without specifying a mode — it returns the walking time and the real "
        "MTR route (origin station + lines + destination station) so you can "
        "answer with directions in one shot. Accepts free-text names (geocoded "
        "via OSM Nominatim + ALS) or lat/lng pairs."
    ),
    args_schema=PlanJourneyArgs,
    result_schema=PlanJourneyResult,
    handler=_journey_handler,
    ttl_seconds=60 * 60,
    budget_ms=3500,
    upstream_langs=frozenset({"en", "zh-Hant"}),
    upstream="smcity.planner",
    scope=ToolScope.DEFAULT,
    domain="any_mode_journey",
)


__all__ = [
    "PLAN_JOURNEY_TOOL",
    "PLAN_WALKING_TOOL",
]
