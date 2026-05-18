"""geo.address_lookup — Lands Department Address Lookup Service (ALS).

Endpoint: https://www.als.gov.hk/lookup
Response format: ALS-specific JSON — `{"SuggestedAddress": [{"Address":
{"PremisesAddress": {...}}}, ...]}`. NOT GeoJSON. See `_handler` for the
exact schema we extract.
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

    # ALS returns its own JSON shape, NOT GeoJSON. Schema (essentials):
    #   data.SuggestedAddress[].Address.PremisesAddress.
    #     EngPremisesAddress  : { BuildingName?, EngStreet, EngDistrict.DcDistrict, Region }
    #     ChiPremisesAddress  : same, 繁體
    #     GeospatialInformation: { Latitude, Longitude, Northing, Easting }
    # v0.4.8 fixes the schema. Prior versions parsed `features[]` (GeoJSON
    # shape we never receive) and silently returned empty candidates.
    candidates: list[AddressCandidate] = []
    for entry in (data.get("SuggestedAddress") or [])[: args.max_results]:
        premises = (entry.get("Address") or {}).get("PremisesAddress") or {}
        eng_block = premises.get("EngPremisesAddress") or {}
        chi_block = premises.get("ChiPremisesAddress") or {}
        geo_info = premises.get("GeospatialInformation") or {}

        eng_name = _compose_name(eng_block, lang="en")
        chi_name = _compose_name(chi_block, lang="tc")
        district = _as_str((eng_block.get("EngDistrict") or {}).get("DcDistrict"))

        lat: float | None = None
        lng: float | None = None
        lat_raw = geo_info.get("Latitude")
        lng_raw = geo_info.get("Longitude")
        if lat_raw and lng_raw:
            try:
                lat = float(lat_raw)
                lng = float(lng_raw)
            except (TypeError, ValueError):
                pass

        candidates.append(
            AddressCandidate(
                name_en=eng_name,
                name_tc=chi_name,
                lat=lat,
                lng=lng,
                district=district,
            )
        )

    return AddressLookupResult(query=args.query, candidates=candidates)


def _compose_name(block: dict[str, object], *, lang: str) -> str | None:
    """Render a user-readable address from an ALS Eng/Chi-PremisesAddress block.

    Order: BuildingName > '<no.> <StreetName>' > StreetName-only.
    """
    if not block:
        return None
    building = _as_str(block.get("BuildingName"))
    if building:
        return building
    street_key = "EngStreet" if lang == "en" else "ChiStreet"
    street_block = block.get(street_key)
    if isinstance(street_block, dict):
        no = _as_str(street_block.get("BuildingNoFrom"))
        street = _as_str(street_block.get("StreetName"))
        if no and street:
            return f"{no} {street}"
        if street:
            return street
    return None


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
