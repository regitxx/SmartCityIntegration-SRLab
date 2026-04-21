"""GMB (Green Minibus) tools — live API at data.etagmb.gov.hk.

GMB API flow requires two HTTP calls:
1. `/route/{region}/{route_code}` → resolves the route_id
2. `/eta/route-stop/{route_id}/{stop_id}` → ETAs at the stop

We collapse both into a single tool for the LLM.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from smcity.tools.registry import ToolContext, ToolSpec, ToolUpstreamError

GMB_BASE = "https://data.etagmb.gov.hk"


def _minutes_until(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    now = datetime.now(dt.tzinfo)
    return max(0, int((dt - now).total_seconds() // 60))


class GmbEtaArgs(BaseModel):
    region: Literal["HKI", "KLN", "NT"] = Field(
        description="GMB region code: HKI (Island), KLN (Kowloon), NT (New Territories)."
    )
    route_code: str = Field(
        min_length=1,
        description="GMB route number as displayed on the minibus (e.g. '1', '23M', '54').",
    )
    stop_id: str = Field(
        min_length=1,
        description="8-digit numeric GMB stop_id. Get from transport.get_gmb_route_info.",
    )


class GmbEtaEntry(BaseModel):
    direction_seq: int
    eta_iso: str | None = None
    minutes_until: int | None = None
    remark_en: str | None = None
    remark_tc: str | None = None


class GmbEtaResult(BaseModel):
    region: str
    route_code: str
    route_id: int
    stop_id: str
    destination_en: str | None = None
    destination_tc: str | None = None
    etas: list[GmbEtaEntry]
    source: str = "data.etagmb.gov.hk"


async def _eta_handler(args: GmbEtaArgs, ctx: ToolContext) -> GmbEtaResult:
    try:
        async with httpx.AsyncClient(timeout=6.0) as h:
            # 1) route lookup
            r = await h.get(f"{GMB_BASE}/route/{args.region}/{args.route_code}")
            r.raise_for_status()
            route_payload = r.json()
            route_data = (route_payload.get("data") or [{}])[0]
            route_id = route_data.get("route_id")
            directions = route_data.get("directions") or []
            dest_en = directions[0].get("dest_en") if directions else None
            dest_tc = directions[0].get("dest_tc") if directions else None

            if route_id is None:
                raise ToolUpstreamError(
                    f"GMB route {args.route_code} not found in region {args.region}."
                )

            # 2) ETA at the specific route + stop
            eta_r = await h.get(f"{GMB_BASE}/eta/route-stop/{route_id}/{args.stop_id}")
            if eta_r.status_code == 404:
                return GmbEtaResult(
                    region=args.region,
                    route_code=args.route_code,
                    route_id=route_id,
                    stop_id=args.stop_id,
                    destination_en=dest_en,
                    destination_tc=dest_tc,
                    etas=[],
                )
            eta_r.raise_for_status()
            eta_payload = eta_r.json()
    except httpx.HTTPError as err:
        raise ToolUpstreamError(f"GMB request failed: {err}") from err

    entries: list[GmbEtaEntry] = []
    for rec in eta_payload.get("data") or []:
        for eta in rec.get("eta") or []:
            iso = eta.get("timestamp")
            entries.append(
                GmbEtaEntry(
                    direction_seq=int(rec.get("route_seq", 1)),
                    eta_iso=iso,
                    minutes_until=_minutes_until(iso),
                    remark_en=eta.get("remarks_en"),
                    remark_tc=eta.get("remarks_tc"),
                )
            )
    entries.sort(key=lambda e: (e.minutes_until is None, e.minutes_until or 999))

    return GmbEtaResult(
        region=args.region,
        route_code=args.route_code,
        route_id=route_id,
        stop_id=args.stop_id,
        destination_en=dest_en,
        destination_tc=dest_tc,
        etas=entries,
    )


GMB_ETA_TOOL: ToolSpec[GmbEtaArgs, GmbEtaResult] = ToolSpec(
    name="transport.get_gmb_eta",
    description_en=(
        "Live ETA for a Green Minibus (GMB) route at a specific stop. Input: "
        "region (HKI / KLN / NT), route_code (e.g. '1', '23M'), 8-digit stop_id. "
        "Requires a known stop_id — if you only have a stop name, ask the user "
        "or defer to the KMB/Citybus tools instead. Returns next arrivals sorted "
        "by ETA, plus the route's destination in EN + 繁體."
    ),
    args_schema=GmbEtaArgs,
    result_schema=GmbEtaResult,
    handler=_eta_handler,
    ttl_seconds=30,
    budget_ms=3000,
    upstream_langs=frozenset({"en", "zh-Hant", "zh-Hans"}),
    upstream="data.etagmb.gov.hk",
)


__all__ = ["GMB_ETA_TOOL"]
