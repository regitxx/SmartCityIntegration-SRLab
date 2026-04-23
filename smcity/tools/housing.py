# ruff: noqa: RUF003
"""Housing tools — HKHA estates, **live** from the Housing Authority API.

v0.4.2 swaps the 10-entry bundled catalogue for the full 241-estate live
feed at `data.housingauthority.gov.hk/psi/rest/export/prh-estates`. The
endpoint is not on CSDI (it's not ArcGIS REST) so it doesn't fit the
generic `csdi.query_features` tool — we have a small dedicated client
here that mirrors the facility-module pattern (module-level cache with
24 h TTL + asyncio lock).

Known trade-off: the live API is **English-only** — it publishes
`Estate_Name` but not a Chinese-name column. We overlay a small
hand-curated `data/hkha_name_map_tc.json` (English → 繁體) for the most
commonly asked estates so Cantonese / Traditional-Chinese fuzzy queries
still hit. Estates not in the map will only match by English name; the
agent's tool description notes this.

Personal application / eligibility questions remain out of scope and the
tool description redirects to the official portal.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field
from rapidfuzz import fuzz, process

from smcity.tools.registry import ToolContext, ToolSpec, ToolUpstreamError

_DATA_ROOT = Path(__file__).resolve().parent.parent.parent / "data"
HKHA_LIVE_URL = "https://data.housingauthority.gov.hk/psi/rest/export/prh-estates"
_CATALOG_TTL_S = 24 * 60 * 60


@cache
def _tc_name_map() -> dict[str, str]:
    """Hand-curated English-name → 繁體-name overlay (skips the _comment key)."""
    raw = json.loads((_DATA_ROOT / "hkha_name_map_tc.json").read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_")}


class HKHAEstate(BaseModel):
    id: str
    name_en: str
    name_tc: str | None = None  # populated from overlay where available
    district: str | None = None
    region: str | None = None
    lat: float
    lng: float
    type: str | None = None  # "Public Rental Housing" / HOS / etc. — as published
    blocks: int | None = None
    flats: int | None = None  # best-effort int parse of No_of_Rental_Flats
    flats_raw: str | None = None  # original string including "as at ..." suffix
    flat_size_m2: str | None = None  # range like "14 – 37"
    year_of_intake: str | None = None
    website: str | None = None


_INT_PREFIX = re.compile(r"^[\s]*([\d][\d\s,]*)")


def _parse_int(value: Any) -> int | None:
    """Parse '8 200 as at 31.12.2025' → 8200; '9' → 9; '' → None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    m = _INT_PREFIX.match(text)
    if not m:
        return None
    digits = m.group(1).replace(" ", "").replace(",", "")
    try:
        return int(digits)
    except ValueError:
        return None


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() in {"N.A.", "N/A", "NIL"}:
        return None
    return text


def _parse_estate(raw: dict[str, Any]) -> HKHAEstate | None:
    name_en = _clean(raw.get("Estate_Name"))
    lat_raw = raw.get("Map_Latitude")
    lng_raw = raw.get("Map_Longitude")
    if not name_en or lat_raw in (None, "") or lng_raw in (None, ""):
        return None
    try:
        lat = float(str(lat_raw))
        lng = float(str(lng_raw))
    except (TypeError, ValueError):
        return None
    flats_raw = _clean(raw.get("No_of_Rental_Flats"))
    website_raw = _clean(raw.get("Estate_Website"))
    # Some rows carry "Yes" instead of a URL; keep either as opaque string.
    return HKHAEstate(
        id=name_en.replace(" ", "_").upper(),
        name_en=name_en,
        name_tc=_tc_name_map().get(name_en),
        district=_clean(raw.get("District_Name")),
        region=_clean(raw.get("Region_Name")),
        lat=lat,
        lng=lng,
        type=_clean(raw.get("Type_of_Estate")),
        blocks=_parse_int(raw.get("No_of_Blocks")),
        flats=_parse_int(flats_raw),
        flats_raw=flats_raw,
        flat_size_m2=_clean(raw.get("Flat_Size_m2")),
        year_of_intake=_clean(raw.get("Year_of_Intake")),
        website=website_raw,
    )


# --- module-level live catalog cache -------------------------------------


@dataclass(slots=True)
class _EstatesCatalog:
    items: list[HKHAEstate] = field(default_factory=list)
    fetched_at: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def fresh(self) -> bool:
        return bool(self.items) and (time.monotonic() - self.fetched_at) < _CATALOG_TTL_S


_catalog = _EstatesCatalog()


def _reset_catalog_for_tests() -> None:
    _catalog.items = []
    _catalog.fetched_at = 0.0


async def _load_estates(*, client: httpx.AsyncClient | None = None) -> list[HKHAEstate]:
    if _catalog.fresh():
        return _catalog.items
    async with _catalog.lock:
        if _catalog.fresh():
            return _catalog.items
        owns = client is None
        http = client or httpx.AsyncClient(timeout=10.0)
        try:
            try:
                resp = await http.get(HKHA_LIVE_URL, params={"format": "json"})
                resp.raise_for_status()
                payload = resp.json()
            except httpx.HTTPError as err:
                raise ToolUpstreamError(f"HKHA fetch failed: {err}") from err
            except ValueError as err:
                raise ToolUpstreamError(f"HKHA non-JSON: {err}") from err
        finally:
            if owns:
                await http.aclose()

        rows = payload.get("data") or []
        parsed = [e for e in (_parse_estate(r) for r in rows) if e is not None]
        _catalog.items = parsed
        _catalog.fetched_at = time.monotonic()
        return parsed


# --- get_estate_info -----------------------------------------------------


class GetEstateInfoArgs(BaseModel):
    name: str = Field(min_length=1, description="Estate name in EN or 繁體 (fuzzy matched).")


class GetEstateInfoResult(BaseModel):
    match: HKHAEstate | None = None
    alternatives: list[HKHAEstate] = Field(default_factory=list)
    source: str = "hkha.live"


async def _estate_handler(args: GetEstateInfoArgs, ctx: ToolContext) -> GetEstateInfoResult:
    rows = await _load_estates()
    aliases = [(alias, r) for r in rows for alias in (r.name_en, r.name_tc) if alias]
    matches = process.extract(
        args.name,
        [a for a, _ in aliases],
        scorer=fuzz.WRatio,
        limit=5,
        score_cutoff=55,
    )
    if not matches:
        return GetEstateInfoResult(match=None, alternatives=[])
    seen: set[str] = set()
    ordered: list[HKHAEstate] = []
    for _, _score, idx in matches:
        r = aliases[idx][1]
        if r.id in seen:
            continue
        seen.add(r.id)
        ordered.append(r)
    best = ordered[0] if ordered else None
    return GetEstateInfoResult(match=best, alternatives=ordered[1:])


GET_ESTATE_INFO_TOOL: ToolSpec[GetEstateInfoArgs, GetEstateInfoResult] = ToolSpec(
    name="housing.get_estate_info",
    description_en=(
        "Look up a Hong Kong Housing Authority estate (Public Rental or HOS) "
        "by name. Live from the Housing Authority open-data API (~241 estates). "
        "The upstream feed is English-only; a small 繁體 name overlay covers "
        "the most common estates so Cantonese queries still match — estates "
        "outside the overlay match by English name. DO NOT claim to check a "
        "user's personal housing application or waiting-list status — that is "
        "not an open API; redirect to "
        "https://www.housingauthority.gov.hk/en/flat-application/"
    ),
    args_schema=GetEstateInfoArgs,
    result_schema=GetEstateInfoResult,
    handler=_estate_handler,
    ttl_seconds=24 * 60 * 60,
    budget_ms=3000,
    upstream_langs=frozenset({"en", "zh-Hant"}),
    upstream="data.housingauthority.gov.hk",
)


# --- list_estates_in_district --------------------------------------------


class ListEstatesArgs(BaseModel):
    district: str = Field(min_length=1, description="District filter, EN (e.g. 'Sha Tin').")
    region: str | None = Field(
        default=None,
        description="Optional region filter: 'Hong Kong Island', 'Kowloon', 'New Territories'.",
    )


class ListEstatesResult(BaseModel):
    district: str
    estates: list[HKHAEstate]
    source: str = "hkha.live"


async def _list_handler(args: ListEstatesArgs, ctx: ToolContext) -> ListEstatesResult:
    rows = await _load_estates()
    d = args.district.lower()
    filtered = [
        r for r in rows if r.district and (r.district.lower() == d or d in r.district.lower())
    ]
    if args.region:
        region = args.region.lower()
        filtered = [r for r in filtered if r.region and region in r.region.lower()]
    return ListEstatesResult(district=args.district, estates=filtered)


LIST_ESTATES_TOOL: ToolSpec[ListEstatesArgs, ListEstatesResult] = ToolSpec(
    name="housing.list_estates_in_district",
    description_en=(
        "List live Hong Kong Housing Authority estates in a given district, "
        "optionally filtered by region ('Hong Kong Island' / 'Kowloon' / "
        "'New Territories'). Covers all ~241 HKHA estates."
    ),
    args_schema=ListEstatesArgs,
    result_schema=ListEstatesResult,
    handler=_list_handler,
    ttl_seconds=24 * 60 * 60,
    budget_ms=3000,
    upstream_langs=frozenset({"en", "zh-Hant"}),
    upstream="data.housingauthority.gov.hk",
)
