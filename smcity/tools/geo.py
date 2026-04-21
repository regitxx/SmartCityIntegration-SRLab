"""geo.address_lookup — Lands Department Address Lookup Service (ALS).

Endpoint: https://www.als.gov.hk/lookup
Response format: GeoJSON FeatureCollection.
"""

from __future__ import annotations

import httpx
from pydantic import BaseModel, Field

from smcity.tools.registry import ToolContext, ToolSpec, ToolUpstreamError

ALS_URL = "https://www.als.gov.hk/lookup"


class AddressLookupArgs(BaseModel):
    query: str = Field(min_length=1, max_length=200, description="free-text address or place in HK")
    max_results: int = Field(default=5, ge=1, le=20)


class AddressCandidate(BaseModel):
    name_en: str | None = None
    name_tc: str | None = None
    lat: float | None = None
    lng: float | None = None
    district: str | None = None


class AddressLookupResult(BaseModel):
    query: str
    candidates: list[AddressCandidate]
    source: str = "als.gov.hk"


async def _handler(args: AddressLookupArgs, ctx: ToolContext) -> AddressLookupResult:
    headers = {
        "Accept": "application/json",
        "Accept-Language": "en,zh-Hant" if ctx.query_lang != "zh-Hant" else "zh-Hant,en",
    }
    params: dict[str, str] = {"q": args.query, "n": str(args.max_results)}
    try:
        async with httpx.AsyncClient(timeout=5.0) as h:
            r = await h.get(ALS_URL, headers=headers, params=params)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as err:
        raise ToolUpstreamError(f"ALS request failed: {err}") from err

    candidates: list[AddressCandidate] = []
    for feature in (data.get("features") or [])[: args.max_results]:
        props = feature.get("properties") or {}
        geom = feature.get("geometry") or {}
        coords = geom.get("coordinates") or []
        # ALS returns a nested structure — address.PremisesAddress.* with both languages.
        eng_addr = (
            props.get("EngPremisesAddress") or props.get("address_en") or props.get("EnglishName")
        )
        chi_addr = (
            props.get("ChiPremisesAddress") or props.get("address_tc") or props.get("ChineseName")
        )
        # Some ALS responses nest under Address.PremisesAddress
        if not (eng_addr or chi_addr):
            inner = (props.get("Address") or {}).get("PremisesAddress") or {}
            eng_block = inner.get("EngPremisesAddress") or {}
            chi_block = inner.get("ChiPremisesAddress") or {}
            eng_addr = eng_block.get("BuildingName") or eng_block.get("StreetName")
            chi_addr = chi_block.get("BuildingName") or chi_block.get("StreetName")
            district = (
                (eng_block.get("District") or {}).get("DcDistrict")
                if isinstance(eng_block.get("District"), dict)
                else eng_block.get("DcDistrict")
            )
        else:
            district = props.get("district") or props.get("DcDistrict")

        lat = None
        lng = None
        if len(coords) == 2:
            lng, lat = float(coords[0]), float(coords[1])
        elif "lat" in props and "lng" in props:
            lat = float(props["lat"])
            lng = float(props["lng"])

        candidates.append(
            AddressCandidate(
                name_en=_as_str(eng_addr),
                name_tc=_as_str(chi_addr),
                lat=lat,
                lng=lng,
                district=_as_str(district),
            )
        )

    return AddressLookupResult(query=args.query, candidates=candidates)


def _as_str(v: object) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v.strip() or None
    return None


ADDRESS_LOOKUP_TOOL: ToolSpec[AddressLookupArgs, AddressLookupResult] = ToolSpec(
    name="geo.address_lookup",
    description_en=(
        "Resolve a free-text Hong Kong address or place name (English or Traditional "
        "Chinese) to structured candidates with lat/lng. Use for any place the user "
        "mentions by name (e.g. 'Sheung Wan', '中環', 'Choi Hung Estate')."
    ),
    args_schema=AddressLookupArgs,
    result_schema=AddressLookupResult,
    handler=_handler,
    ttl_seconds=60 * 60,
    budget_ms=2000,
    upstream_langs=frozenset({"en", "zh-Hant"}),
    upstream="als.gov.hk",
)
