"""LCSD facility tools — basketball courts + swimming pools.

Phase 1b ships against a **bundled static catalog** (docs/research/02_*.md §2)
drawn from LCSD CSDI dataset listings. Phase 2 will swap to the live CSDI
GeoJSON endpoint once we've normalised the coord-system gotcha.
"""

from __future__ import annotations

import json
import math
from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from rapidfuzz import fuzz, process

from smcity.tools.registry import ToolContext, ToolSpec

_DATA_ROOT = Path(__file__).resolve().parent.parent.parent / "data"


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# --- shared models --------------------------------------------------------


class BasketballCourt(BaseModel):
    id: str
    name_en: str
    name_tc: str
    district: str
    lat: float
    lng: float
    courts: int
    floodlit: bool
    outdoor: bool
    booking: Literal["free", "smartplay"]


class SwimmingPool(BaseModel):
    id: str
    name_en: str
    name_tc: str
    district: str
    lat: float
    lng: float
    indoor: bool
    heated: bool
    lanes: int


@cache
def _load_courts() -> list[BasketballCourt]:
    raw = json.loads((_DATA_ROOT / "lcsd_basketball_courts.json").read_text(encoding="utf-8"))
    return [BasketballCourt.model_validate(r) for r in raw]


@cache
def _load_pools() -> list[SwimmingPool]:
    raw = json.loads((_DATA_ROOT / "lcsd_swimming_pools.json").read_text(encoding="utf-8"))
    return [SwimmingPool.model_validate(r) for r in raw]


# --- find_nearby_courts --------------------------------------------------


class FindNearbyCourtsArgs(BaseModel):
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    district: str | None = Field(
        default=None,
        description="Optional HK district filter (case-insensitive, English). e.g. 'Sha Tin'.",
    )
    name_query: str | None = Field(
        default=None, description="Optional fuzzy name match across EN/繁體 names."
    )
    max_results: int = Field(default=5, ge=1, le=20)
    radius_km: float = Field(default=3.0, ge=0.1, le=30.0)


class NearbyCourt(BaseModel):
    id: str
    name_en: str
    name_tc: str
    district: str
    lat: float
    lng: float
    courts: int
    floodlit: bool
    outdoor: bool
    booking: Literal["free", "smartplay"]
    distance_m: int | None = None


class FindNearbyCourtsResult(BaseModel):
    query: FindNearbyCourtsArgs
    courts: list[NearbyCourt]
    source: str = "lcsd.csdi (bundled)"


async def _find_courts(args: FindNearbyCourtsArgs, ctx: ToolContext) -> FindNearbyCourtsResult:
    rows = _load_courts()
    filtered: list[BasketballCourt] = list(rows)

    if args.district:
        d = args.district.lower()
        filtered = [r for r in filtered if r.district.lower() == d or d in r.district.lower()]

    if args.name_query:
        all_names = [(alias, r) for r in filtered for alias in (r.name_en, r.name_tc) if alias]
        matches = process.extract(
            args.name_query,
            [a for a, _ in all_names],
            scorer=fuzz.WRatio,
            limit=args.max_results * 3,
            score_cutoff=65,
        )
        seen: set[str] = set()
        filtered = []
        for _, _s, idx in matches:
            r = all_names[idx][1]
            if r.id in seen:
                continue
            seen.add(r.id)
            filtered.append(r)

    if args.lat is not None and args.lng is not None:
        scored = [(r, _haversine_km(args.lat, args.lng, r.lat, r.lng) * 1000) for r in filtered]
        scored = [row for row in scored if row[1] <= args.radius_km * 1000]
        scored.sort(key=lambda row: row[1])
        nearby = [
            NearbyCourt(**row[0].model_dump(), distance_m=round(row[1]))
            for row in scored[: args.max_results]
        ]
    else:
        nearby = [
            NearbyCourt(**r.model_dump(), distance_m=None) for r in filtered[: args.max_results]
        ]

    return FindNearbyCourtsResult(query=args, courts=nearby)


FIND_NEARBY_COURTS_TOOL: ToolSpec[FindNearbyCourtsArgs, FindNearbyCourtsResult] = ToolSpec(
    name="facility.find_nearby_courts",
    description_en=(
        "Find LCSD basketball courts. Either provide lat/lng + radius (km), a "
        "district name, or a fuzzy name_query — or combine them. Most are free "
        "outdoor courts; a few require SmartPLAY booking (marked)."
    ),
    args_schema=FindNearbyCourtsArgs,
    result_schema=FindNearbyCourtsResult,
    handler=_find_courts,
    ttl_seconds=24 * 60 * 60,
    budget_ms=200,
    upstream_langs=frozenset({"en", "zh-Hant"}),
    upstream="lcsd.csdi (bundled)",
)


# --- find_nearby_pools ---------------------------------------------------


class FindNearbyPoolsArgs(BaseModel):
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    district: str | None = None
    indoor_only: bool = Field(default=False)
    max_results: int = Field(default=5, ge=1, le=20)
    radius_km: float = Field(default=3.0, ge=0.1, le=30.0)


class NearbyPool(BaseModel):
    id: str
    name_en: str
    name_tc: str
    district: str
    lat: float
    lng: float
    indoor: bool
    heated: bool
    lanes: int
    distance_m: int | None = None


class FindNearbyPoolsResult(BaseModel):
    pools: list[NearbyPool]
    source: str = "lcsd.csdi (bundled)"


async def _find_pools(args: FindNearbyPoolsArgs, ctx: ToolContext) -> FindNearbyPoolsResult:
    rows = _load_pools()
    filtered = list(rows)
    if args.district:
        d = args.district.lower()
        filtered = [r for r in filtered if r.district.lower() == d or d in r.district.lower()]
    if args.indoor_only:
        filtered = [r for r in filtered if r.indoor]
    if args.lat is not None and args.lng is not None:
        scored = [(r, _haversine_km(args.lat, args.lng, r.lat, r.lng) * 1000) for r in filtered]
        scored = [row for row in scored if row[1] <= args.radius_km * 1000]
        scored.sort(key=lambda row: row[1])
        nearby = [
            NearbyPool(**row[0].model_dump(), distance_m=round(row[1]))
            for row in scored[: args.max_results]
        ]
    else:
        nearby = [
            NearbyPool(**r.model_dump(), distance_m=None) for r in filtered[: args.max_results]
        ]
    return FindNearbyPoolsResult(pools=nearby)


FIND_NEARBY_POOLS_TOOL: ToolSpec[FindNearbyPoolsArgs, FindNearbyPoolsResult] = ToolSpec(
    name="facility.find_nearby_pools",
    description_en=(
        "Find LCSD public swimming pools. Filter by lat/lng + radius, district, "
        "and/or indoor_only. Note many outdoor pools are seasonal (closed Nov-Mar)."
    ),
    args_schema=FindNearbyPoolsArgs,
    result_schema=FindNearbyPoolsResult,
    handler=_find_pools,
    ttl_seconds=24 * 60 * 60,
    budget_ms=200,
    upstream_langs=frozenset({"en", "zh-Hant"}),
    upstream="lcsd.csdi (bundled)",
)
