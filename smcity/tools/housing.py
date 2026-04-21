"""Housing tools — HKHA estates (bundled) + safe-wording redirects.

v0.1 ships lookups + aggregate info only. Personal application / eligibility
questions are deliberately NOT handled — the agent redirects to the official
eligibility checker.
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


class HKHAEstate(BaseModel):
    id: str
    name_en: str
    name_tc: str
    district: str
    region: str
    lat: float
    lng: float
    type: Literal["PRH", "HOS"]
    blocks: int
    flats: int
    year: int


@cache
def _load_estates() -> list[HKHAEstate]:
    raw = json.loads((_DATA_ROOT / "hkha_estates.json").read_text(encoding="utf-8"))
    return [HKHAEstate.model_validate(r) for r in raw]


# --- get_estate_info -----------------------------------------------------


class GetEstateInfoArgs(BaseModel):
    name: str = Field(min_length=1, description="Estate name in EN or 繁體 (fuzzy matched).")


class GetEstateInfoResult(BaseModel):
    match: HKHAEstate | None = None
    alternatives: list[HKHAEstate] = Field(default_factory=list)
    source: str = "hkha (bundled)"


async def _estate_handler(args: GetEstateInfoArgs, ctx: ToolContext) -> GetEstateInfoResult:
    rows = _load_estates()
    names = [(alias, r) for r in rows for alias in (r.name_en, r.name_tc) if alias]
    matches = process.extract(
        args.name,
        [a for a, _ in names],
        scorer=fuzz.WRatio,
        limit=5,
        score_cutoff=55,
    )
    if not matches:
        return GetEstateInfoResult(match=None, alternatives=[])
    seen: set[str] = set()
    ordered: list[HKHAEstate] = []
    for _, _score, idx in matches:
        r = names[idx][1]
        if r.id in seen:
            continue
        seen.add(r.id)
        ordered.append(r)
    best = ordered[0] if ordered else None
    return GetEstateInfoResult(match=best, alternatives=ordered[1:])


GET_ESTATE_INFO_TOOL: ToolSpec[GetEstateInfoArgs, GetEstateInfoResult] = ToolSpec(
    name="housing.get_estate_info",
    description_en=(
        "Look up a Hong Kong Housing Authority estate (Public Rental or Home "
        "Ownership Scheme) by name. Returns the best match plus alternatives. Use "
        "for general estate information. DO NOT claim to check a user's personal "
        "housing application or waiting-list status — that is not an open API; "
        "direct them to https://www.housingauthority.gov.hk/en/flat-application/"
    ),
    args_schema=GetEstateInfoArgs,
    result_schema=GetEstateInfoResult,
    handler=_estate_handler,
    ttl_seconds=24 * 60 * 60,
    budget_ms=200,
    upstream_langs=frozenset({"en", "zh-Hant"}),
    upstream="hkha (bundled)",
)


# --- list_estates_in_district --------------------------------------------


class ListEstatesArgs(BaseModel):
    district: str = Field(min_length=1, description="District filter, EN (e.g. 'Sha Tin').")
    region: str | None = Field(
        default=None,
        description="Optional region filter: 'HK Island', 'Kowloon', 'N.T.'.",
    )


class ListEstatesResult(BaseModel):
    district: str
    estates: list[HKHAEstate]
    source: str = "hkha (bundled)"


async def _list_handler(args: ListEstatesArgs, ctx: ToolContext) -> ListEstatesResult:
    d = args.district.lower()
    rows = [r for r in _load_estates() if r.district.lower() == d or d in r.district.lower()]
    if args.region:
        region = args.region.lower()
        rows = [r for r in rows if r.region.lower() == region]
    return ListEstatesResult(district=args.district, estates=rows)


LIST_ESTATES_TOOL: ToolSpec[ListEstatesArgs, ListEstatesResult] = ToolSpec(
    name="housing.list_estates_in_district",
    description_en=(
        "List Hong Kong Housing Authority estates (PRH and HOS) in a given district, "
        "optionally filtered by region."
    ),
    args_schema=ListEstatesArgs,
    result_schema=ListEstatesResult,
    handler=_list_handler,
    ttl_seconds=24 * 60 * 60,
    budget_ms=200,
    upstream_langs=frozenset({"en", "zh-Hant"}),
    upstream="hkha (bundled)",
)
