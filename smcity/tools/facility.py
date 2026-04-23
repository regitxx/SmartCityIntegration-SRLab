"""LCSD facility tools — basketball courts + swimming pools.

**Live data source.** v0.3.4 swaps the bundled JSON catalogs for live CSDI
ArcGIS FeatureServer queries via `smcity.tools.csdi.query_feature_server`.
Coverage grows from 15 bundled courts / 10 bundled pools to ~305 courts /
~46 pools — full Hong Kong footprint, always fresh.

Trade-offs versus the bundled snapshot:
- We no longer expose hand-curated `floodlit` / `outdoor` / `booking`
  fields (CSDI doesn't publish them). Courts are assumed outdoor unless
  the LLM can infer from name/address.
- `indoor_only` pool filter is removed — `FacilityDetailsEN` only mentions
  "Indoor" for ~1/46 pools in practice, so the filter would be misleading.

The catalog is held in a module-level cache (`_CourtsCatalog` /
`_PoolsCatalog`) with a 24 h TTL and an asyncio lock so the first call of
each process fetches, subsequent calls hit RAM. Upstream failures bubble
up as `ToolUpstreamError` like any other live data tool.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field
from rapidfuzz import fuzz, process

from smcity.geometry import haversine_km as _haversine_km
from smcity.tools.csdi import CSDI_DATASETS, query_feature_server
from smcity.tools.registry import ToolContext, ToolSpec, ToolUpstreamError

# --- shared models --------------------------------------------------------


class BasketballCourt(BaseModel):
    id: str
    name_en: str
    name_tc: str
    address_en: str | None = None
    address_tc: str | None = None
    district: str | None = None
    lat: float
    lng: float
    courts: int | None = None  # number of basketball courts at this sports ground


class SwimmingPool(BaseModel):
    id: str
    name_en: str
    name_tc: str
    address_en: str | None = None
    address_tc: str | None = None
    district: str | None = None
    lat: float
    lng: float
    facility_type: str | None = None
    opening_hours: str | None = None
    telephone: str | None = None


# --- CSDI catalog caches -------------------------------------------------

_CATALOG_TTL_S = 24 * 60 * 60
_COURTS_PAGE_SIZE = 200  # the real transfer cap is lower than the advertised 2000
_POOLS_PAGE_SIZE = 200


@dataclass(slots=True)
class _Catalog[T]:
    items: list[T] = field(default_factory=list)
    fetched_at: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def fresh(self, *, ttl_s: float = _CATALOG_TTL_S) -> bool:
        return bool(self.items) and (time.monotonic() - self.fetched_at) < ttl_s


_courts_catalog: _Catalog[BasketballCourt] = _Catalog()
_pools_catalog: _Catalog[SwimmingPool] = _Catalog()


def _title(s: str | None) -> str | None:
    """Normalise CSDI's uppercase districts ('SHA TIN') to title case ('Sha Tin')."""
    if not s:
        return None
    return " ".join(word.capitalize() for word in s.split())


def _clean(s: Any) -> str | None:
    """Drop obvious placeholder values that LCSD uses in place of nulls."""
    if s is None:
        return None
    text = str(s).strip()
    if not text or text.upper() in {"N.A.", "N/A", "NIL"}:
        return None
    return text


def _parse_court(attrs: dict[str, Any], lat: float, lng: float) -> BasketballCourt:
    count_raw = attrs.get("No__of_Basketball_Courts_EN")
    try:
        courts = int(count_raw) if count_raw is not None else None
    except (TypeError, ValueError):
        courts = None
    return BasketballCourt(
        id=str(attrs.get("OBJECTID", f"{lat:.4f},{lng:.4f}")),
        name_en=_clean(attrs.get("NAME_EN")) or "(no name)",
        name_tc=_clean(attrs.get("NAME_TC")) or "",
        address_en=_clean(attrs.get("ADDRESS_EN")),
        address_tc=_clean(attrs.get("ADDRESS_TC")),
        district=_title(_clean(attrs.get("SEARCH01_EN"))),
        lat=lat,
        lng=lng,
        courts=courts,
    )


def _parse_pool(attrs: dict[str, Any], lat: float, lng: float) -> SwimmingPool:
    return SwimmingPool(
        id=str(attrs.get("OBJECTID", f"{lat:.4f},{lng:.4f}")),
        name_en=_clean(attrs.get("NameEN")) or "(no name)",
        name_tc=_clean(attrs.get("NameTC")) or "",
        address_en=_clean(attrs.get("AddressEN")),
        address_tc=_clean(attrs.get("AddressTC")),
        district=_title(_clean(attrs.get("DistrictEN"))),
        lat=lat,
        lng=lng,
        facility_type=_clean(attrs.get("FacilityTypeEN")),
        opening_hours=_clean(attrs.get("OpeningHoursEN")),
        telephone=_clean(attrs.get("TelephoneEN")),
    )


async def _load_courts() -> list[BasketballCourt]:
    if _courts_catalog.fresh():
        return _courts_catalog.items
    async with _courts_catalog.lock:
        if _courts_catalog.fresh():
            return _courts_catalog.items
        ds = CSDI_DATASETS["lcsd_basketball_courts"]
        result = await query_feature_server(
            ds.url,
            out_fields="NAME_EN,NAME_TC,ADDRESS_EN,ADDRESS_TC,SEARCH01_EN,"
            "No__of_Basketball_Courts_EN,OBJECTID",
            page_size=_COURTS_PAGE_SIZE,
            limit=2000,
        )
        parsed = [
            _parse_court(f.attributes, f.lat, f.lng)
            for f in result.features
            if f.lat is not None and f.lng is not None
        ]
        # Dataset contains all sports grounds; keep only those that actually
        # have at least one basketball court.
        _courts_catalog.items = [c for c in parsed if (c.courts or 0) > 0]
        _courts_catalog.fetched_at = time.monotonic()
        return _courts_catalog.items


async def _load_pools() -> list[SwimmingPool]:
    if _pools_catalog.fresh():
        return _pools_catalog.items
    async with _pools_catalog.lock:
        if _pools_catalog.fresh():
            return _pools_catalog.items
        ds = CSDI_DATASETS["lcsd_swimming_pools"]
        result = await query_feature_server(
            ds.url,
            out_fields="NameEN,NameTC,AddressEN,AddressTC,DistrictEN,"
            "FacilityTypeEN,OpeningHoursEN,TelephoneEN,OBJECTID",
            page_size=_POOLS_PAGE_SIZE,
            limit=2000,
        )
        _pools_catalog.items = [
            _parse_pool(f.attributes, f.lat, f.lng)
            for f in result.features
            if f.lat is not None and f.lng is not None
        ]
        _pools_catalog.fetched_at = time.monotonic()
        return _pools_catalog.items


def _reset_catalogs_for_tests() -> None:
    """Called by tests that want to force a re-fetch."""
    _courts_catalog.items = []
    _courts_catalog.fetched_at = 0.0
    _pools_catalog.items = []
    _pools_catalog.fetched_at = 0.0


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
    address_en: str | None = None
    address_tc: str | None = None
    district: str | None = None
    lat: float
    lng: float
    courts: int | None = None
    distance_m: int | None = None


class FindNearbyCourtsResult(BaseModel):
    query: FindNearbyCourtsArgs
    courts: list[NearbyCourt]
    source: str = "lcsd.csdi (live)"


def _filter_by_district[T: (BasketballCourt, SwimmingPool)](
    rows: list[T], district: str
) -> list[T]:
    d = district.lower()
    return [r for r in rows if r.district and (r.district.lower() == d or d in r.district.lower())]


def _filter_by_name[T: (BasketballCourt, SwimmingPool)](
    rows: list[T], query: str, max_results: int
) -> list[T]:
    aliases = [(alias, r) for r in rows for alias in (r.name_en, r.name_tc) if alias]
    if not aliases:
        return []
    matches = process.extract(
        query,
        [a for a, _ in aliases],
        scorer=fuzz.WRatio,
        limit=max_results * 3,
        score_cutoff=65,
    )
    seen: set[str] = set()
    ordered: list[T] = []
    for _, _s, idx in matches:
        r = aliases[idx][1]
        if r.id in seen:
            continue
        seen.add(r.id)
        ordered.append(r)
    return ordered


async def _find_courts(args: FindNearbyCourtsArgs, ctx: ToolContext) -> FindNearbyCourtsResult:
    try:
        rows = await _load_courts()
    except ToolUpstreamError:
        raise
    except Exception as err:  # pragma: no cover — httpx/json failures surface as upstream
        raise ToolUpstreamError(f"LCSD basketball fetch failed: {err}") from err

    filtered: list[BasketballCourt] = list(rows)
    if args.district:
        filtered = _filter_by_district(filtered, args.district)
    if args.name_query:
        filtered = _filter_by_name(filtered, args.name_query, args.max_results)

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
        "Find LCSD basketball courts (live from data.gov.hk CSDI — ~300 HK-wide "
        "sports grounds with basketball courts). Filter by lat/lng + radius, "
        "district name (e.g. 'Sha Tin', 'Kwai Tsing'), and/or fuzzy name_query. "
        "Returns bilingual names, address, district, and the per-venue court count."
    ),
    args_schema=FindNearbyCourtsArgs,
    result_schema=FindNearbyCourtsResult,
    handler=_find_courts,
    ttl_seconds=24 * 60 * 60,
    budget_ms=3000,
    upstream_langs=frozenset({"en", "zh-Hant"}),
    upstream="portal.csdi.gov.hk (lcsd basketball courts)",
)


# --- find_nearby_pools ---------------------------------------------------


class FindNearbyPoolsArgs(BaseModel):
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    district: str | None = None
    name_query: str | None = Field(
        default=None, description="Optional fuzzy name match across EN/繁體 names."
    )
    max_results: int = Field(default=5, ge=1, le=20)
    radius_km: float = Field(default=3.0, ge=0.1, le=30.0)


class NearbyPool(BaseModel):
    id: str
    name_en: str
    name_tc: str
    address_en: str | None = None
    address_tc: str | None = None
    district: str | None = None
    lat: float
    lng: float
    facility_type: str | None = None
    opening_hours: str | None = None
    telephone: str | None = None
    distance_m: int | None = None


class FindNearbyPoolsResult(BaseModel):
    query: FindNearbyPoolsArgs
    pools: list[NearbyPool]
    source: str = "lcsd.csdi (live)"


async def _find_pools(args: FindNearbyPoolsArgs, ctx: ToolContext) -> FindNearbyPoolsResult:
    try:
        rows = await _load_pools()
    except ToolUpstreamError:
        raise
    except Exception as err:  # pragma: no cover
        raise ToolUpstreamError(f"LCSD pools fetch failed: {err}") from err

    filtered: list[SwimmingPool] = list(rows)
    if args.district:
        filtered = _filter_by_district(filtered, args.district)
    if args.name_query:
        filtered = _filter_by_name(filtered, args.name_query, args.max_results)

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
    return FindNearbyPoolsResult(query=args, pools=nearby)


FIND_NEARBY_POOLS_TOOL: ToolSpec[FindNearbyPoolsArgs, FindNearbyPoolsResult] = ToolSpec(
    name="facility.find_nearby_pools",
    description_en=(
        "Find LCSD public swimming pools (live from data.gov.hk CSDI — ~46 HK-wide "
        "pools). Filter by lat/lng + radius, district name, and/or fuzzy name_query. "
        "Returns bilingual names, address, district, facility type, opening hours, "
        "and telephone. Many outdoor pools are seasonal (closed Nov-Mar)."
    ),
    args_schema=FindNearbyPoolsArgs,
    result_schema=FindNearbyPoolsResult,
    handler=_find_pools,
    ttl_seconds=24 * 60 * 60,
    budget_ms=3000,
    upstream_langs=frozenset({"en", "zh-Hant"}),
    upstream="portal.csdi.gov.hk (lcsd swimming pools)",
)
