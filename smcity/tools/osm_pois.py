# ruff: noqa: RUF002  # en-dash in docstring prose is intentional.
"""OpenStreetMap POI search via Overpass API.

Covers 30 POI categories from the project's Selected Smart City Data Maps
workbook (S514–S549) behind a single tool. Each category maps to one or more
OSM tag filters. Results are deduplicated and trimmed to a configurable max.

Why OSM not data.gov.hk: for these POI categories (convenience stores,
toilets, MTR entrances, public elevators, dentists, benches, …) there is no
single HK-government dataset with the full coverage. The Selected list
explicitly routes these to Overpass. That's what we ship.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from smcity.tools.registry import ToolContext, ToolSpec, ToolUpstreamError

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Hong Kong bounding box (SW lat, SW lng, NE lat, NE lng).
_HK_BBOX = (22.15, 113.83, 22.58, 114.44)


# Category → list of Overpass tag filters. Each filter matches the
# "(node|way)[key=value]" pattern Overpass expects. Covers every entry from
# the project workbook's POI + road-facility sections.
@dataclass(slots=True, frozen=True)
class _TagSpec:
    keys: list[tuple[str, str | None]]  # (key, value or None for presence check)
    # When value is None, we match any presence of the key (e.g. `bench=*`).


_CATEGORIES: dict[str, _TagSpec] = {
    # Shops
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
    # Amenities
    "public_toilet": _TagSpec([("amenity", "toilets")]),
    "place_of_worship": _TagSpec([("amenity", "place_of_worship")]),
    "recycling_location": _TagSpec([("amenity", "recycling")]),
    "veterinarian": _TagSpec([("amenity", "veterinary")]),
    "marketplace": _TagSpec([("amenity", "marketplace")]),
    "drinking_water": _TagSpec([("amenity", "drinking_water")]),
    "government_office": _TagSpec([("office", "government")]),
    "dentist": _TagSpec([("amenity", "dentist"), ("healthcare", "dentist")]),
    # Infrastructure / road facilities
    "mtr_station_entrance": _TagSpec([("railway", "subway_entrance")]),
    "public_elevator": _TagSpec([("highway", "elevator")]),
    "bench": _TagSpec([("amenity", "bench"), ("bench", "yes")]),
    "shelter": _TagSpec([("amenity", "shelter"), ("shelter", "yes")]),
    "handrail": _TagSpec([("handrail", "yes")]),
}


Category = Literal[
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


class SearchOsmArgs(BaseModel):
    category: Category = Field(description="POI category — must be one of the 30 supported kinds.")
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


class SearchOsmResult(BaseModel):
    category: Category
    bbox_used: tuple[float, float, float, float]
    pois: list[OsmPoi]
    source: str = "openstreetmap.org (Overpass)"


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


async def _handler(args: SearchOsmArgs, ctx: ToolContext) -> SearchOsmResult:
    if (
        args.min_lat is not None
        and args.min_lng is not None
        and args.max_lat is not None
        and args.max_lng is not None
    ):
        bbox = (args.min_lat, args.min_lng, args.max_lat, args.max_lng)
    elif args.lat is not None and args.lng is not None:
        bbox = _bbox_from_point(args.lat, args.lng, args.radius_m)
    else:
        bbox = _HK_BBOX

    query = _build_query(args.category, bbox)
    try:
        async with httpx.AsyncClient(timeout=20.0) as h:
            r = await h.post(
                OVERPASS_URL,
                data={"data": query},
                headers={"User-Agent": "smcity-hk-agent/0.3"},
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

    return SearchOsmResult(category=args.category, bbox_used=bbox, pois=pois)


SEARCH_OSM_POIS_TOOL: ToolSpec[SearchOsmArgs, SearchOsmResult] = ToolSpec(
    name="geo.search_osm_pois",
    description_en=(
        "Search OpenStreetMap for points of interest in Hong Kong. Covers 30 "
        "categories from the project's Selected Smart City Data Maps: shops "
        "(convenience / supermarket / hardware / hairdresser / clothes / "
        "electronics / department / variety / houseware / beauty / optician / "
        "shoes / greengrocer / bookstore / laundry / kiosk / bookmaker), "
        "amenities (public_toilet / place_of_worship / recycling_location / "
        "veterinarian / marketplace / drinking_water / government_office / "
        "dentist), infrastructure (mtr_station_entrance / public_elevator / "
        "bench / shelter / handrail). Accepts a lat/lng + radius, a bbox, "
        "or neither (defaults to all of HK). Returns up to max_results "
        "deduplicated POIs with coords + name/brand/opening_hours when "
        "tagged. Use when the user asks about any of those place kinds — "
        "do NOT use this for MTR/KMB/Citybus (those have live operator APIs)."
    ),
    args_schema=SearchOsmArgs,
    result_schema=SearchOsmResult,
    handler=_handler,
    ttl_seconds=60 * 60,  # cached; OSM data churns slowly
    budget_ms=8000,  # Overpass can be slow
    upstream_langs=frozenset({"en", "zh-Hant", "zh-Hans"}),
    upstream="overpass-api.de",
)


__all__ = ["SEARCH_OSM_POIS_TOOL", "Category"]
