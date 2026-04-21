"""Cross-operator stop search tools.

- transport.find_stops_near_point — k-NN over KMB stop catalog + MTR stations
- transport.find_stops_by_name    — fuzzy stop-name search across KMB + MTR

Citybus is excluded from the proximity index for now (no list-all-stops API).
"""

from __future__ import annotations

import math

from pydantic import BaseModel, Field
from rapidfuzz import fuzz, process

from smcity.tools.registry import ToolContext, ToolSpec
from smcity.tools.transport import _load_stations
from smcity.tools.transport_kmb import KMBStop, kmb_catalog

# --- helpers -------------------------------------------------------------


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# MTR stations don't carry live lat/lng in our static catalog. We keep a small
# lookup of station-code → (lat, lng) for the busiest ~30 stations. Others are
# served via KMB stops that co-locate with them.
MTR_STATION_COORDS: dict[str, tuple[float, float]] = {
    "CEN": (22.2820, 114.1582),  # Central
    "ADM": (22.2797, 114.1648),  # Admiralty
    "WAC": (22.2775, 114.1730),  # Wan Chai
    "CAB": (22.2802, 114.1849),  # Causeway Bay
    "NOP": (22.2913, 114.2007),  # North Point
    "QUB": (22.2884, 114.2098),  # Quarry Bay
    "SHW": (22.2863, 114.1515),  # Sheung Wan
    "SYP": (22.2854, 114.1425),  # Sai Ying Pun
    "HKU": (22.2838, 114.1350),  # HKU
    "KET": (22.2814, 114.1289),  # Kennedy Town
    "TST": (22.2979, 114.1722),  # Tsim Sha Tsui
    "JOR": (22.3050, 114.1717),  # Jordan
    "YMT": (22.3131, 114.1708),  # Yau Ma Tei
    "MOK": (22.3195, 114.1692),  # Mong Kok
    "PRE": (22.3247, 114.1685),  # Prince Edward
    "SSP": (22.3310, 114.1622),  # Sham Shui Po
    "KOT": (22.3371, 114.1761),  # Kowloon Tong
    "DIH": (22.3400, 114.2013),  # Diamond Hill
    "CHH": (22.3348, 114.2087),  # Choi Hung
    "KOB": (22.3229, 114.2143),  # Kowloon Bay
    "KWT": (22.3121, 114.2261),  # Kwun Tong
    "TKO": (22.3076, 114.2600),  # Tseung Kwan O
    "SHT": (22.3817, 114.1870),  # Sha Tin
    "TAW": (22.3727, 114.1787),  # Tai Wai
    "FOT": (22.3953, 114.1978),  # Fo Tan
    "UNI": (22.4149, 114.2098),  # University
    "TAP": (22.4453, 114.1702),  # Tai Po Market
    "HUH": (22.3032, 114.1816),  # Hung Hom
    "HOK": (22.2849, 114.1587),  # Hong Kong
    "KOW": (22.3039, 114.1614),  # Kowloon
    "AIR": (22.3157, 113.9369),  # Airport
    "TUC": (22.2891, 113.9415),  # Tung Chung
}


# --- find_stops_near_point -----------------------------------------------


class FindStopsNearPointArgs(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    radius_m: int = Field(
        default=500, ge=50, le=5000, description="Search radius in metres (default 500 m)."
    )
    max_results: int = Field(default=8, ge=1, le=30)
    operators: list[str] = Field(
        default_factory=lambda: ["kmb", "mtr"],
        description="Which operators to include: 'kmb', 'mtr'. Citybus omitted in v0.1.",
    )


class NearbyStop(BaseModel):
    operator: str  # "kmb" | "mtr"
    id: str
    name_en: str
    name_tc: str | None = None
    lat: float
    lng: float
    distance_m: int


class FindStopsNearPointResult(BaseModel):
    query_lat: float
    query_lng: float
    stops: list[NearbyStop]
    source: str = "kmb+mtr (local)"


async def _near_handler(args: FindStopsNearPointArgs, ctx: ToolContext) -> FindStopsNearPointResult:
    ops = {op.lower() for op in args.operators}
    candidates: list[NearbyStop] = []
    radius_km = args.radius_m / 1000.0

    if "kmb" in ops:
        stops: list[KMBStop] = await kmb_catalog().all()
        for s in stops:
            d_km = _haversine_km(args.lat, args.lng, s.lat, s.lng)
            if d_km > radius_km:
                continue
            candidates.append(
                NearbyStop(
                    operator="kmb",
                    id=s.stop_id,
                    name_en=s.name_en,
                    name_tc=s.name_tc or None,
                    lat=s.lat,
                    lng=s.lng,
                    distance_m=round(d_km * 1000),
                )
            )

    if "mtr" in ops:
        for st in _load_stations():
            coords = MTR_STATION_COORDS.get(st.code)
            if not coords:
                continue
            lat, lng = coords
            d_km = _haversine_km(args.lat, args.lng, lat, lng)
            if d_km > radius_km:
                continue
            candidates.append(
                NearbyStop(
                    operator="mtr",
                    id=st.code,
                    name_en=st.names.get("en", ""),
                    name_tc=st.names.get("zh-Hant") or None,
                    lat=lat,
                    lng=lng,
                    distance_m=round(d_km * 1000),
                )
            )

    candidates.sort(key=lambda c: c.distance_m)
    return FindStopsNearPointResult(
        query_lat=args.lat,
        query_lng=args.lng,
        stops=candidates[: args.max_results],
    )


FIND_STOPS_NEAR_POINT_TOOL: ToolSpec[FindStopsNearPointArgs, FindStopsNearPointResult] = ToolSpec(
    name="transport.find_stops_near_point",
    description_en=(
        "Find KMB and MTR stops within a radius (m) of a lat/lng. Always call "
        "geo.address_lookup first to turn a place name into coordinates, then "
        "pass the resolved lat/lng here. Returns up to max_results stops sorted "
        "by distance."
    ),
    args_schema=FindStopsNearPointArgs,
    result_schema=FindStopsNearPointResult,
    handler=_near_handler,
    ttl_seconds=60 * 60,
    budget_ms=1500,
    upstream_langs=frozenset({"en", "zh-Hant", "zh-Hans"}),
    upstream="kmb+mtr",
)


# --- find_stops_by_name --------------------------------------------------


class FindStopsByNameArgs(BaseModel):
    query: str = Field(min_length=1, max_length=120)
    operators: list[str] = Field(default_factory=lambda: ["kmb", "mtr"])
    max_results: int = Field(default=8, ge=1, le=30)


class FindStopsByNameResult(BaseModel):
    query: str
    stops: list[NearbyStop]
    source: str = "kmb+mtr (local)"


async def _by_name_handler(args: FindStopsByNameArgs, ctx: ToolContext) -> FindStopsByNameResult:
    ops = {op.lower() for op in args.operators}
    entries: list[tuple[str, NearbyStop]] = []

    if "kmb" in ops:
        for s in await kmb_catalog().all():
            for alias in (s.name_en, s.name_tc, s.name_sc):
                if alias:
                    entries.append(
                        (
                            alias,
                            NearbyStop(
                                operator="kmb",
                                id=s.stop_id,
                                name_en=s.name_en,
                                name_tc=s.name_tc or None,
                                lat=s.lat,
                                lng=s.lng,
                                distance_m=0,
                            ),
                        )
                    )
    if "mtr" in ops:
        for st in _load_stations():
            coords = MTR_STATION_COORDS.get(st.code, (0.0, 0.0))
            for alias in st.names.values():
                if alias:
                    entries.append(
                        (
                            alias,
                            NearbyStop(
                                operator="mtr",
                                id=st.code,
                                name_en=st.names.get("en", ""),
                                name_tc=st.names.get("zh-Hant") or None,
                                lat=coords[0],
                                lng=coords[1],
                                distance_m=0,
                            ),
                        )
                    )

    names_only = [e[0] for e in entries]
    matches = process.extract(
        args.query, names_only, scorer=fuzz.WRatio, limit=args.max_results * 3
    )
    seen: set[tuple[str, str]] = set()
    out: list[NearbyStop] = []
    for _, _score, idx in matches:
        stop = entries[idx][1]
        key = (stop.operator, stop.id)
        if key in seen:
            continue
        seen.add(key)
        out.append(stop)
        if len(out) >= args.max_results:
            break
    return FindStopsByNameResult(query=args.query, stops=out)


FIND_STOPS_BY_NAME_TOOL: ToolSpec[FindStopsByNameArgs, FindStopsByNameResult] = ToolSpec(
    name="transport.find_stops_by_name",
    description_en=(
        "Fuzzy-search KMB stops and MTR stations by name (EN / 繁體 / 简体). Returns "
        "the top candidates across both operators — use when the user names a place "
        "but no coordinates are known yet."
    ),
    args_schema=FindStopsByNameArgs,
    result_schema=FindStopsByNameResult,
    handler=_by_name_handler,
    ttl_seconds=60 * 60,
    budget_ms=800,
    upstream_langs=frozenset({"en", "zh-Hant", "zh-Hans"}),
    upstream="kmb+mtr",
)
