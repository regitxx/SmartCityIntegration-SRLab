# ruff: noqa: RUF003  # en-dashes in workbook range comments (S514–S530) are intentional.
"""OpenStreetMap POI search via Overpass API — one thin tool per category.

The old `geo.search_osm_pois` mega-tool packed 30 POI kinds behind a single
`category` Literal. The LLM had to (1) pick this tool, (2) pick the right
enum value from 30 string options buried in description prose. That second
step is where the model hallucinated or fell back to general knowledge —
particularly for niche kinds (bench, kiosk, dentist, handrail).

We export 30 named tools instead — `geo.find_dentist`, `geo.find_bench`,
`geo.find_convenience_store`, … — generated from the same `_CATEGORIES`
table. Tool routing is the operation frontier models are actually trained
to do well; string-enum routing is the operation they hallucinate on.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

import httpx
from pydantic import BaseModel, Field, model_validator

from smcity.tools.registry import ToolContext, ToolSpec, ToolUpstreamError

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Hong Kong bounding box (SW lat, SW lng, NE lat, NE lng).
_HK_BBOX = (22.15, 113.83, 22.58, 114.44)


# Category → list of Overpass tag filters. Each filter matches the
# "(node|way)[key=value]" pattern Overpass expects.
@dataclass(slots=True, frozen=True)
class _TagSpec:
    keys: list[tuple[str, str | None]]  # (key, value or None for presence check)


_CATEGORIES: dict[str, _TagSpec] = {
    # Shops (S514–S530 in the project workbook)
    "convenience_store": _TagSpec([("shop", "convenience")]),
    "supermarket": _TagSpec([("shop", "supermarket")]),
    "hardware_store": _TagSpec([("shop", "hardware")]),
    "hairdresser": _TagSpec([("shop", "hairdresser")]),
    "clothes_shop": _TagSpec([("shop", "clothes")]),
    "electronics_shop": _TagSpec([("shop", "electronics")]),
    "department_store": _TagSpec([("shop", "department_store")]),
    "variety_store": _TagSpec([("shop", "variety_store")]),
    "houseware_shop": _TagSpec([("shop", "houseware")]),
    "beauty_shop": _TagSpec([("shop", "beauty")]),
    "optician": _TagSpec([("shop", "optician")]),
    "shoe_shop": _TagSpec([("shop", "shoes")]),
    "greengrocer": _TagSpec([("shop", "greengrocer")]),
    "bookstore": _TagSpec([("shop", "books")]),
    "laundry": _TagSpec([("shop", "laundry")]),
    "kiosk": _TagSpec([("shop", "kiosk")]),
    "bookmaker": _TagSpec([("shop", "bookmaker")]),
    # Amenities (S531–S540)
    "public_toilet": _TagSpec([("amenity", "toilets")]),
    "place_of_worship": _TagSpec([("amenity", "place_of_worship")]),
    "recycling_location": _TagSpec([("amenity", "recycling")]),
    "veterinarian": _TagSpec([("amenity", "veterinary")]),
    "marketplace": _TagSpec([("amenity", "marketplace")]),
    "drinking_water": _TagSpec([("amenity", "drinking_water")]),
    "government_office": _TagSpec([("office", "government")]),
    "dentist": _TagSpec([("amenity", "dentist"), ("healthcare", "dentist")]),
    # Infrastructure / road facilities (S541–S549)
    "mtr_station_entrance": _TagSpec([("railway", "subway_entrance")]),
    "public_elevator": _TagSpec([("highway", "elevator")]),
    "bench": _TagSpec([("amenity", "bench"), ("bench", "yes")]),
    "shelter": _TagSpec([("amenity", "shelter"), ("shelter", "yes")]),
    "handrail": _TagSpec([("handrail", "yes")]),
}

# Human-readable label per slug — used in each tool's description so the
# LLM sees "Find dentists near a point" instead of `find_dentist`.
_LABELS: dict[str, str] = {
    "convenience_store": "convenience stores (7-Eleven, Circle K, VanGO, etc.)",
    "supermarket": "supermarkets",
    "hardware_store": "hardware stores",
    "hairdresser": "hairdressers and barbershops",
    "clothes_shop": "clothing shops",
    "electronics_shop": "electronics shops",
    "department_store": "department stores",
    "variety_store": "variety / dollar stores (Japan Home, 多多, etc.)",
    "houseware_shop": "houseware shops",
    "beauty_shop": "beauty supply / cosmetics shops",
    "optician": "opticians",
    "shoe_shop": "shoe shops",
    "greengrocer": "greengrocers / fruit-and-vegetable shops",
    "bookstore": "bookstores",
    "laundry": "laundromats and dry cleaners",
    "kiosk": "kiosks and news stands",
    "bookmaker": "betting shops (Jockey Club off-course branches)",
    "public_toilet": "public toilets",
    "place_of_worship": "places of worship (temples, churches, mosques)",
    "recycling_location": "recycling collection points",
    "veterinarian": "veterinarians / animal clinics",
    "marketplace": "wet markets and open marketplaces",
    "drinking_water": "public drinking water fountains",
    "government_office": "government offices",
    "dentist": "dentists",
    "mtr_station_entrance": "MTR station entrances and exits",
    "public_elevator": "public elevators (street / footbridge lifts)",
    "bench": "public benches",
    "shelter": "public shelters and awnings",
    "handrail": "public handrails and railings",
}


# --- argument + result schemas (shared across all 30 tools) ---------------


class FindPoiArgs(BaseModel):
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
        # The chain_rules engine then auto-completes address_lookup -> find_X
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
    source: str = "openstreetmap.org (Overpass)"


# --- Overpass query construction ------------------------------------------


def _build_query(category: str, bbox: tuple[float, float, float, float]) -> str:
    spec = _CATEGORIES[category]
    lines: list[str] = []
    bbox_str = f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}"
    for key, value in spec.keys:
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


async def _search_one_category(
    category: str, args: FindPoiArgs, _ctx: ToolContext
) -> FindPoiResult:
    bbox = _resolve_bbox(args)
    query = _build_query(category, bbox)
    try:
        async with httpx.AsyncClient(timeout=20.0) as h:
            r = await h.post(
                OVERPASS_URL,
                data={"data": query},
                headers={"User-Agent": "smcity-hk-agent/0.5"},
            )
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as err:
        raise ToolUpstreamError(f"Overpass API failed: {err}") from err

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
                tags={
                    k: str(v)
                    for k, v in tags.items()
                    if k
                    in {
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
                },
            )
        )
        if len(pois) >= args.max_results:
            break

    return FindPoiResult(category=category, bbox_used=bbox, pois=pois)


# --- factory: one ToolSpec per category -----------------------------------


def _make_handler(category: str) -> Callable[[FindPoiArgs, ToolContext], Awaitable[FindPoiResult]]:
    """Bind the category to a closure so each ToolSpec has its own handler.

    Done as a top-level function (not a comprehension lambda) so the binding
    is explicit and unambiguous — each call creates a fresh closure over its
    own `category` argument.
    """

    async def _handler(args: FindPoiArgs, ctx: ToolContext) -> FindPoiResult:
        return await _search_one_category(category, args, ctx)

    return _handler


def _make_poi_tool(slug: str) -> ToolSpec[FindPoiArgs, FindPoiResult]:
    # Description is intentionally terse. The previous form averaged ~45
    # words per tool * 30 tools = ~1.3K tokens of duplicated prose in every
    # prompt. With gpt-oss-120b that prompt-processing tax was breaking
    # tool-calling on transport datasets (v0.5.0 regression). The label
    # (from `_LABELS`) carries the discriminative info; result-shape detail
    # is already in the schema; chain enforcement is in `smcity/chain_rules.py`
    # rather than this prose.
    label = _LABELS[slug]
    return ToolSpec(
        name=f"geo.find_{slug}",
        description_en=(
            f"Find {label} near a lat/lng (Hong Kong). "
            "Pair with `geo.address_lookup` for landmark queries."
        ),
        args_schema=FindPoiArgs,
        result_schema=FindPoiResult,
        handler=_make_handler(slug),
        ttl_seconds=60 * 60,  # OSM data churns slowly
        budget_ms=8000,  # Overpass can be slow
        upstream_langs=frozenset({"en", "zh-Hant", "zh-Hans"}),
        upstream="overpass-api.de",
    )


OSM_POI_TOOLS: list[ToolSpec[FindPoiArgs, FindPoiResult]] = [
    _make_poi_tool(slug) for slug in _CATEGORIES
]


# Public mapping: slug → full tool name. Used by smcity_fuzz.contracts to
# express "the right tool for OSM category X is geo.find_X" without each
# contract having to hard-code 30 strings.
POI_TOOL_NAME: dict[str, str] = {slug: f"geo.find_{slug}" for slug in _CATEGORIES}

# Reverse lookup for the orchestrator's chain-completion check (Fix 3).
POI_TOOL_NAMES: frozenset[str] = frozenset(POI_TOOL_NAME.values())


__all__ = [
    "OSM_POI_TOOLS",
    "POI_TOOL_NAME",
    "POI_TOOL_NAMES",
    "FindPoiArgs",
    "FindPoiResult",
    "OsmPoi",
]
