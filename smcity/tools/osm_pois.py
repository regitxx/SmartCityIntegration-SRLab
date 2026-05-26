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

from dataclasses import dataclass
from typing import Literal

import httpx
from pydantic import BaseModel, Field, model_validator

from smcity.tools.registry import ToolContext, ToolScope, ToolSpec, ToolUpstreamError

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


# The `category` field's description carries the bilingual EN + 繁體 hint
# list — single source of truth so the LLM has the same routing info it
# used to get from 30 separate tool descriptions. Order matches the order
# in `_CATEGORIES` to keep the prompt prefix deterministic.
_CATEGORY_HINTS: dict[str, str] = {
    "convenience_store": "便利店 / 7-Eleven / Circle K / VanGO",
    "supermarket": "超市 / 超級市場 (Wellcome, Park'n Shop, AEON)",
    "hardware_store": "五金舖 / 五金店",
    "hairdresser": "髮型屋 / 理髮店 / 髮廊",
    "clothes_shop": "服裝店 / 衫舖",
    "electronics_shop": "電器店 / 電子產品店",
    "department_store": "百貨公司 (SOGO, Yata, Lane Crawford)",
    "variety_store": "雜貨店 / 日本城 / 多多",
    "houseware_shop": "家品店 / 家居用品店",
    "beauty_shop": "美妝店 / 化妝品店 (SaSa, Bonjour)",
    "optician": "眼鏡舖",
    "shoe_shop": "鞋舖 / 鞋店",
    "greengrocer": "生果舖 / 蔬果店",
    "bookstore": "書店 / 書局",
    "laundry": "洗衣店 / 乾洗店",
    "kiosk": "報攤 / 小賣亭",
    "bookmaker": "馬會投注站 (off-course Jockey Club)",
    "public_toilet": "公廁 / 公共廁所 / 洗手間",
    "place_of_worship": "廟宇 / 教堂 / 寺廟 / 清真寺",
    "recycling_location": "回收站 / 回收箱 / 回收點",
    "veterinarian": "獸醫 / 動物診所",
    "marketplace": "街市 / 菜市場",
    "drinking_water": "飲水機 / 公眾飲水器",
    "government_office": "政府辦事處 / 民政事務處",
    "dentist": "牙醫 / 牙科診所",
    "mtr_station_entrance": "港鐵 / 地鐵出入口",
    "public_elevator": "公共升降機 / 街道電梯",
    "bench": "公眾長凳 / 休憩座椅",
    "shelter": "公眾遮蔭處 / 涼亭 / 巴士站候車亭",
    "handrail": "扶手 / 公眾欄杆",
}


def _build_category_field_description() -> str:
    """One bilingual index of every category slug → user-facing meaning.

    Built at import time from `_CATEGORY_HINTS` so the table is the single
    source of truth. Lives on the `category` field's description so the LLM
    sees the slug↔meaning mapping in JSON Schema next to the enum it has to
    pick from — that adjacency is what makes the collapse work as well as
    the 30-separate-descriptions form did.
    """
    parts = [f"{slug} ({hint})" for slug, hint in _CATEGORY_HINTS.items()]
    return (
        "Category of POI to search for. Pick ONE slug from: "
        + "; ".join(parts)
        + "."
    )


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
    category: PoiCategory = Field(description=_build_category_field_description())
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


async def _find_poi_handler(args: FindPoiArgs, _ctx: ToolContext) -> FindPoiResult:
    bbox = _resolve_bbox(args)
    query = _build_query(args.category, bbox)
    try:
        async with httpx.AsyncClient(timeout=20.0) as h:
            r = await h.post(
                OVERPASS_URL,
                data={"data": query},
                headers={"User-Agent": "smcity-hk-agent/0.6"},
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

    return FindPoiResult(category=args.category, bbox_used=bbox, pois=pois)


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
