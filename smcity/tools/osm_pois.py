# ruff: noqa: RUF003  # en-dashes in workbook range comments (S514–S530) are intentional.
"""OpenStreetMap POI search via Overpass API — single tool, category enum.

v0.6.0 collapsed the 30 per-category POI tools (`geo.find_dentist`,
`geo.find_convenience_store`, …) into one `geo.find_poi` tool whose
`category` arg is a `Literal` over the same 30 slugs. The 30 individual
tools shared one args schema (`FindPoiArgs`) and one handler shape, so
exposing them as 30 OpenAI function schemas duplicated ~12K tokens of
identical parameter prose in every prompt. With one tool the LLM sees
the categories as a JSON-Schema enum (which gpt-oss-120b reads natively),
and the prompt drops from ~19K tokens to ~7K.

Routing accuracy is preserved by:
- the `category` field's bilingual description (EN + 繁體 hints per slug),
- the chain-rules POI engine (`smcity/chain_rules.py`), which still maps
  user text → category slug for the `address_lookup` → `find_poi` chain.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field, model_validator

from smcity.data.poi_store import get_poi_store
from smcity.settings import get_settings
from smcity.tools.poi_categories import CATEGORIES, category_field_description
from smcity.tools.registry import ToolContext, ToolScope, ToolSpec, ToolUpstreamError

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Hong Kong bounding box (SW lat, SW lng, NE lat, NE lng).
_HK_BBOX = (22.15, 113.83, 22.58, 114.44)

# Citation source labels — distinct so an A/B sweep can tell which path served a
# given POI result. Both are OpenStreetMap data; "local mirror" means it came
# from the nightly SQLite snapshot, "Overpass" means a live API call.
SOURCE_MIRROR = "openstreetmap.org (local mirror)"
SOURCE_LIVE = "openstreetmap.org (Overpass)"

# OSM tag keys surfaced on each POI. Shared by the live parse and the nightly
# refresh (which reuses `_parse_overpass_elements`) so the mirror and the live
# path expose the same fields — no drift between them.
_KEPT_TAG_KEYS: frozenset[str] = frozenset(
    {
        "name",
        "name:en",
        "name:zh",
        "brand",
        "operator",
        "opening_hours",
        "wheelchair",
        "website",
        "phone",
        "addr:street",
        "addr:housenumber",
        "religion",
    }
)

# Backward-compatible alias — the canonical category data now lives in
# `smcity/tools/poi_categories.py::CATEGORIES`. Kept so existing imports and
# tests that reference `_CATEGORIES` keep working; iterate over `.keys()` for
# slugs and `CATEGORIES[slug].tags` for the Overpass filters.
_CATEGORIES = CATEGORIES


PoiCategory = Literal[
    "convenience_store",
    "supermarket",
    "hardware_store",
    "hairdresser",
    "clothes_shop",
    "electronics_shop",
    "department_store",
    "variety_store",
    "houseware_shop",
    "beauty_shop",
    "optician",
    "shoe_shop",
    "greengrocer",
    "bookstore",
    "laundry",
    "kiosk",
    "bookmaker",
    "public_toilet",
    "place_of_worship",
    "recycling_location",
    "veterinarian",
    "marketplace",
    "drinking_water",
    "government_office",
    "dentist",
    "mtr_station_entrance",
    "public_elevator",
    "bench",
    "shelter",
    "handrail",
]


# Defensive runtime check — the Literal above and `_CATEGORIES` must stay
# in lockstep, otherwise an LLM-emitted slug could pass schema validation
# but blow up in `_build_query`. Fail loudly at import time instead.
_LITERAL_VALUES: frozenset[str] = frozenset(PoiCategory.__args__)  # type: ignore[attr-defined]
_CATEGORY_VALUES: frozenset[str] = frozenset(_CATEGORIES)
if _CATEGORY_VALUES != _LITERAL_VALUES:  # pragma: no cover — startup invariant
    missing = _LITERAL_VALUES ^ _CATEGORY_VALUES
    raise RuntimeError(
        f"PoiCategory Literal and _CATEGORIES disagree on slugs: {sorted(missing)}"
    )


# --- argument + result schemas --------------------------------------------


class FindPoiArgs(BaseModel):
    category: PoiCategory = Field(description=category_field_description())
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    radius_m: int = Field(
        default=800,
        ge=50,
        le=5000,
        description="Search radius in metres when lat/lng is given (default 800 m).",
    )
    max_results: int = Field(default=20, ge=1, le=100)
    min_lat: float | None = Field(default=None, description="Optional bbox — SW corner lat.")
    min_lng: float | None = Field(default=None, description="Optional bbox — SW corner lng.")
    max_lat: float | None = Field(default=None, description="Optional bbox — NE corner lat.")
    max_lng: float | None = Field(default=None, description="Optional bbox — NE corner lng.")

    @model_validator(mode="after")
    def _require_spatial_scope(self) -> FindPoiArgs:
        # Reject the "search all of HK" shape that gpt-oss-120b emits when it
        # skips `geo.address_lookup` and passes null lat/lng with no bbox
        # (observed in v0.5.4 live smoke). Forces the LLM to either provide a
        # point or an explicit bbox; either path is structurally correct.
        # The chain_rules engine then auto-completes address_lookup -> find_poi
        # in the common case.
        has_point = self.lat is not None and self.lng is not None
        has_full_bbox = (
            self.min_lat is not None
            and self.min_lng is not None
            and self.max_lat is not None
            and self.max_lng is not None
        )
        if not has_point and not has_full_bbox:
            raise ValueError(
                "either (lat AND lng) or a full bbox (min_lat, min_lng, "
                "max_lat, max_lng) must be provided. Call geo.address_lookup "
                "first to resolve a place name to coordinates."
            )
        return self


class OsmPoi(BaseModel):
    osm_type: Literal["node", "way", "relation"]
    osm_id: int
    lat: float
    lng: float
    name: str | None = None
    name_en: str | None = None
    name_zh: str | None = None
    address: str | None = None
    tags: dict[str, str] = Field(default_factory=dict)


class FindPoiResult(BaseModel):
    category: str  # the slug, e.g. "dentist"
    bbox_used: tuple[float, float, float, float]
    pois: list[OsmPoi]
    source: str = SOURCE_LIVE


# --- Overpass query construction ------------------------------------------


def _build_query(category: str, bbox: tuple[float, float, float, float]) -> str:
    spec = CATEGORIES[category]
    lines: list[str] = []
    bbox_str = f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}"
    for key, value in spec.tags:
        filt = f'["{key}"]' if value is None else f'["{key}"="{value}"]'
        lines.append(f"  node{filt}({bbox_str});")
        lines.append(f"  way{filt}({bbox_str});")
        lines.append(f"  relation{filt}({bbox_str});")
    body = "\n".join(lines)
    return f"[out:json][timeout:25];\n(\n{body}\n);\nout center 200;"


def _bbox_from_point(lat: float, lng: float, radius_m: int) -> tuple[float, float, float, float]:
    # Rough conversion (1° lat ~ 111 km, 1° lng at 22° lat ~ 103 km).
    dlat = radius_m / 111_000
    dlng = radius_m / 103_000
    return (lat - dlat, lng - dlng, lat + dlat, lng + dlng)


def _resolve_bbox(args: FindPoiArgs) -> tuple[float, float, float, float]:
    if (
        args.min_lat is not None
        and args.min_lng is not None
        and args.max_lat is not None
        and args.max_lng is not None
    ):
        return (args.min_lat, args.min_lng, args.max_lat, args.max_lng)
    if args.lat is not None and args.lng is not None:
        return _bbox_from_point(args.lat, args.lng, args.radius_m)
    return _HK_BBOX


def _parse_overpass_elements(
    data: dict[str, Any], max_results: int | None = None
) -> list[OsmPoi]:
    """Turn a raw Overpass `out center` payload into `OsmPoi` rows.

    Single source of truth for OSM-element -> POI shaping, used by BOTH the live
    `find_poi` fallback and the nightly `poi_refresh` mirror build. Centralising
    it here is deliberate: when the live path and the mirror derive their fields
    from the same code, the mirror cannot silently diverge from what a live
    query would have returned (the drift class v0.7.1 closed for category tags).

    `max_results=None` keeps every element — the refresh wants the full set;
    the live handler passes its cap.
    """
    pois: list[OsmPoi] = []
    seen: set[tuple[str, int]] = set()
    for el in data.get("elements") or []:
        osm_type = el.get("type")
        osm_id = el.get("id")
        if osm_type not in {"node", "way", "relation"} or osm_id is None:
            continue
        key = (osm_type, int(osm_id))
        if key in seen:
            continue
        seen.add(key)

        if osm_type == "node":
            lat = el.get("lat")
            lng = el.get("lon")
        else:
            center = el.get("center") or {}
            lat = center.get("lat")
            lng = center.get("lon")
        if lat is None or lng is None:
            continue

        tags = el.get("tags") or {}
        pois.append(
            OsmPoi(
                osm_type=osm_type,
                osm_id=int(osm_id),
                lat=float(lat),
                lng=float(lng),
                name=tags.get("name"),
                name_en=tags.get("name:en"),
                name_zh=tags.get("name:zh") or tags.get("name:zh-Hant") or tags.get("name:zh-Hans"),
                address=tags.get("addr:full") or tags.get("addr:street"),
                tags={k: str(v) for k, v in tags.items() if k in _KEPT_TAG_KEYS},
            )
        )
        if max_results is not None and len(pois) >= max_results:
            break

    return pois


async def fetch_overpass(query: str) -> dict[str, Any]:
    """POST an Overpass query and return the parsed JSON, or raise upstream.

    Shared by the live tool fallback and the nightly refresh so both speak to
    `overpass-api.de` with identical timeout/headers semantics.
    """
    try:
        async with httpx.AsyncClient(timeout=20.0) as h:
            r = await h.post(
                OVERPASS_URL,
                data={"data": query},
                headers={"User-Agent": "smcity-hk-agent/0.7"},
            )
            r.raise_for_status()
            data: dict[str, Any] = r.json()
            return data
    except httpx.HTTPError as err:
        raise ToolUpstreamError(f"Overpass API failed: {err}") from err


async def _find_poi_live(
    category: str, bbox: tuple[float, float, float, float], max_results: int
) -> list[OsmPoi]:
    """Query live Overpass for one category within a bbox (the fallback path)."""
    data = await fetch_overpass(_build_query(category, bbox))
    return _parse_overpass_elements(data, max_results)


async def _find_poi_handler(args: FindPoiArgs, _ctx: ToolContext) -> FindPoiResult:
    bbox = _resolve_bbox(args)
    settings = get_settings()

    # Local mirror first. `is_populated` is true once a category has been
    # refreshed (even to zero rows), so a genuinely-empty category does not loop
    # back to live Overpass forever. A store error degrades to the fallback
    # rather than failing the turn.
    if settings.poi_store_enabled:
        store = get_poi_store()
        try:
            populated = await asyncio.to_thread(store.is_populated, args.category)
            if populated:
                rows = await asyncio.to_thread(
                    store.query, args.category, bbox, args.max_results
                )
                pois = [OsmPoi(**row) for row in rows]
                return FindPoiResult(
                    category=args.category,
                    bbox_used=bbox,
                    pois=pois,
                    source=SOURCE_MIRROR,
                )
        except Exception:  # mirror is best-effort; fall through to live
            if not settings.poi_overpass_fallback:
                raise

    if not settings.poi_overpass_fallback:
        # A/B isolation mode: no mirror hit and fallback disabled -> empty result
        # (an honest "mirror has nothing", not a live call that masks the gap).
        return FindPoiResult(
            category=args.category, bbox_used=bbox, pois=[], source=SOURCE_MIRROR
        )

    pois = await _find_poi_live(args.category, bbox, args.max_results)
    return FindPoiResult(
        category=args.category, bbox_used=bbox, pois=pois, source=SOURCE_LIVE
    )


# --- the single ToolSpec --------------------------------------------------


# Public constants used by chain_rules, langrouter coverage, fuzz contracts,
# tests. `POI_TOOL` is the registered tool name; `POI_CATEGORIES` is the set
# of valid `category` arg values. `POI_TOOL_NAMES` is preserved (as a single-
# element frozenset) so chain_rules' `required_successor_names` API doesn't
# need a separate code path.
POI_TOOL: str = "geo.find_poi"
POI_CATEGORIES: frozenset[str] = frozenset(_CATEGORIES.keys())
POI_TOOL_NAMES: frozenset[str] = frozenset({POI_TOOL})


FIND_POI_TOOL: ToolSpec[FindPoiArgs, FindPoiResult] = ToolSpec(
    name=POI_TOOL,
    description_en=(
        "Find points of interest near a lat/lng (Hong Kong). Specify a "
        "`category` — covers shops (convenience_store, supermarket, "
        "bookstore, …), amenities (public_toilet, dentist, place_of_worship, "
        "…) and infrastructure (mtr_station_entrance, public_elevator, "
        "bench, …). See the `category` field for the full bilingual list. "
        "Pair with `geo.address_lookup` for landmark queries."
    ),
    args_schema=FindPoiArgs,
    result_schema=FindPoiResult,
    handler=_find_poi_handler,
    ttl_seconds=60 * 60,  # OSM data churns slowly
    budget_ms=8000,  # Overpass can be slow
    upstream_langs=frozenset({"en", "zh-Hant", "zh-Hans"}),
    upstream="overpass-api.de",
    scope=ToolScope.DEFAULT,
    domain="osm_poi",
    citation_discriminator_key="category",
)


__all__ = [
    "FIND_POI_TOOL",
    "POI_CATEGORIES",
    "POI_TOOL",
    "POI_TOOL_NAMES",
    "FindPoiArgs",
    "FindPoiResult",
    "OsmPoi",
    "PoiCategory",
]
