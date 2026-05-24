"""KMB / LWB tools — data.etabus.gov.hk.

Tools shipped:
- transport.get_kmb_eta_by_stop     — all ETAs at a specific KMB stop
- transport.get_kmb_eta_by_route_stop — ETA for one route at one stop
- transport.resolve_kmb_stop        — fuzzy lookup by name (internal helper,
                                       also exposed as a tool so the LLM can
                                       use it explicitly if it wants)

Stop catalog is fetched once per process on first use and cached.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime

import httpx
from pydantic import BaseModel, Field
from rapidfuzz import fuzz, process

from smcity.tools.registry import ToolContext, ToolScope, ToolSpec, ToolUpstreamError

KMB_BASE = "https://data.etabus.gov.hk/v1/transport/kmb"


# --- stop catalog (lazy, in-memory) --------------------------------------


@dataclass(slots=True)
class KMBStop:
    stop_id: str
    name_en: str
    name_tc: str
    name_sc: str
    lat: float
    lng: float


class _StopCatalog:
    """Lazy KMB stop catalog. One HTTP call on first use, then in-memory."""

    def __init__(self) -> None:
        self._stops: list[KMBStop] | None = None
        self._by_id: dict[str, KMBStop] = {}
        self._lock = asyncio.Lock()

    async def ensure_loaded(self) -> None:
        if self._stops is not None:
            return
        async with self._lock:
            if self._stops is not None:
                return
            try:
                async with httpx.AsyncClient(timeout=15.0) as h:
                    r = await h.get(f"{KMB_BASE}/stop")
                    r.raise_for_status()
                    payload = r.json()
            except httpx.HTTPError as err:
                raise ToolUpstreamError(f"KMB stop catalog fetch failed: {err}") from err
            data = payload.get("data") or []
            stops: list[KMBStop] = []
            for rec in data:
                try:
                    stops.append(
                        KMBStop(
                            stop_id=str(rec["stop"]),
                            name_en=str(rec.get("name_en") or ""),
                            name_tc=str(rec.get("name_tc") or ""),
                            name_sc=str(rec.get("name_sc") or ""),
                            lat=float(rec["lat"]),
                            lng=float(rec["long"]),
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    continue
            self._stops = stops
            self._by_id = {s.stop_id: s for s in stops}

    async def all(self) -> list[KMBStop]:
        await self.ensure_loaded()
        assert self._stops is not None
        return self._stops

    async def by_id(self, stop_id: str) -> KMBStop | None:
        await self.ensure_loaded()
        return self._by_id.get(stop_id)


_catalog = _StopCatalog()


def kmb_catalog() -> _StopCatalog:
    return _catalog


async def resolve_kmb_stop(name: str, *, limit: int = 5) -> list[KMBStop]:
    """Fuzzy-match a stop name against every KMB stop (EN/繁體/简体)."""
    stops = await _catalog.all()
    rows: list[tuple[str, KMBStop]] = []
    for s in stops:
        for alias in (s.name_en, s.name_tc, s.name_sc):
            if alias:
                rows.append((alias, s))
                rows.append((alias.lower(), s))
    names_only = [r[0] for r in rows]
    matches = process.extract(name, names_only, scorer=fuzz.WRatio, limit=limit * 4)
    seen: dict[str, KMBStop] = {}
    out: list[KMBStop] = []
    for _, _score, idx in matches:
        stop = rows[idx][1]
        if stop.stop_id in seen:
            continue
        seen[stop.stop_id] = stop
        out.append(stop)
        if len(out) >= limit:
            break
    return out


# --- get_kmb_eta_by_stop -------------------------------------------------


class KMBEtaByStopArgs(BaseModel):
    stop_name_or_id: str = Field(
        min_length=1,
        description=(
            "Either a KMB stop_id (16 hex chars) or a stop name in EN / 繁體 / 简体 "
            "(fuzzy matched). Names are resolved to the best match; ambiguous names "
            "return ETAs for the best candidate."
        ),
    )


class KMBEtaEntry(BaseModel):
    route: str
    direction: str  # "O" outbound / "I" inbound
    service_type: int
    destination_en: str
    destination_tc: str
    eta_iso: str | None = None
    minutes_until: int | None = None
    remark_en: str | None = None


class KMBEtaByStopResult(BaseModel):
    stop_id: str
    stop_name_en: str
    stop_name_tc: str
    lat: float
    lng: float
    etas: list[KMBEtaEntry]
    source: str = "data.etabus.gov.hk/kmb"


def _minutes_until(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    now = datetime.now(dt.tzinfo)
    return max(0, int((dt - now).total_seconds() // 60))


async def _eta_handler(args: KMBEtaByStopArgs, ctx: ToolContext) -> KMBEtaByStopResult:
    catalog = kmb_catalog()
    raw = args.stop_name_or_id.strip()

    stop: KMBStop | None = None
    # 16-char hex heuristic → stop_id
    if len(raw) == 16 and all(c in "0123456789ABCDEFabcdef" for c in raw):
        stop = await catalog.by_id(raw.upper())
    if stop is None:
        candidates = await resolve_kmb_stop(raw, limit=1)
        if not candidates:
            raise ToolUpstreamError(f"no KMB stop matched {raw!r}")
        stop = candidates[0]

    try:
        async with httpx.AsyncClient(timeout=5.0) as h:
            r = await h.get(f"{KMB_BASE}/stop-eta/{stop.stop_id}")
            r.raise_for_status()
            payload = r.json()
    except httpx.HTTPError as err:
        raise ToolUpstreamError(f"KMB stop-eta failed: {err}") from err

    etas: list[KMBEtaEntry] = []
    for rec in payload.get("data") or []:
        eta_iso = rec.get("eta")
        etas.append(
            KMBEtaEntry(
                route=str(rec.get("route", "")),
                direction=str(rec.get("dir", "")),
                service_type=int(rec.get("service_type", 1)),
                destination_en=str(rec.get("dest_en", "")),
                destination_tc=str(rec.get("dest_tc", "")),
                eta_iso=eta_iso,
                minutes_until=_minutes_until(eta_iso),
                remark_en=rec.get("rmk_en") or None,
            )
        )

    # Order by soonest first (None last).
    etas.sort(key=lambda e: (e.minutes_until is None, e.minutes_until or 999))

    return KMBEtaByStopResult(
        stop_id=stop.stop_id,
        stop_name_en=stop.name_en,
        stop_name_tc=stop.name_tc,
        lat=stop.lat,
        lng=stop.lng,
        etas=etas[:10],
    )


KMB_ETA_BY_STOP_TOOL: ToolSpec[KMBEtaByStopArgs, KMBEtaByStopResult] = ToolSpec(
    name="transport.get_kmb_eta_by_stop",
    description_en=(
        "ETAs for every KMB (or LWB) route calling at a given stop. Accepts either "
        "a 16-char KMB stop_id or a free-text stop name in EN / 繁體 / 简体 (fuzzy-"
        "matched against the live KMB stop catalog). Returns next ~10 buses sorted "
        "by ETA. Use whenever the user asks about KMB / LWB buses from a specific "
        "stop, bus terminus, or street. Do NOT use for Citybus, GMB minibus, or "
        "MTR — those have their own operator-specific tools."
    ),
    args_schema=KMBEtaByStopArgs,
    result_schema=KMBEtaByStopResult,
    handler=_eta_handler,
    ttl_seconds=30,
    budget_ms=2000,
    upstream_langs=frozenset({"en", "zh-Hant", "zh-Hans"}),
    upstream="data.etabus.gov.hk/kmb",
    scope=ToolScope.SPECIALIZED,
    domain="kmb_lwb_bus_only",
)


# --- get_kmb_eta_by_route_stop -------------------------------------------


class KMBEtaByRouteStopArgs(BaseModel):
    route: str = Field(min_length=1, description="KMB route number, e.g. '1A', '81K'.")
    stop_name_or_id: str = Field(
        min_length=1,
        description="KMB stop_id or stop name (EN / 繁體 / 简体; fuzzy matched).",
    )
    service_type: int = Field(default=1, ge=1, le=9, description="Service type; 1 = normal.")


class KMBRouteEtaResult(BaseModel):
    stop_id: str
    stop_name_en: str
    stop_name_tc: str
    route: str
    service_type: int
    etas: list[KMBEtaEntry]
    source: str = "data.etabus.gov.hk/kmb"


async def _route_stop_handler(args: KMBEtaByRouteStopArgs, ctx: ToolContext) -> KMBRouteEtaResult:
    catalog = kmb_catalog()
    raw = args.stop_name_or_id.strip()
    stop: KMBStop | None = None
    if len(raw) == 16 and all(c in "0123456789ABCDEFabcdef" for c in raw):
        stop = await catalog.by_id(raw.upper())
    if stop is None:
        candidates = await resolve_kmb_stop(raw, limit=1)
        if not candidates:
            raise ToolUpstreamError(f"no KMB stop matched {raw!r}")
        stop = candidates[0]

    url = f"{KMB_BASE}/eta/{stop.stop_id}/{args.route}/{args.service_type}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as h:
            r = await h.get(url)
            r.raise_for_status()
            payload = r.json()
    except httpx.HTTPError as err:
        raise ToolUpstreamError(f"KMB route-eta failed: {err}") from err

    etas: list[KMBEtaEntry] = []
    for rec in payload.get("data") or []:
        eta_iso = rec.get("eta")
        etas.append(
            KMBEtaEntry(
                route=str(rec.get("route", args.route)),
                direction=str(rec.get("dir", "")),
                service_type=int(rec.get("service_type", args.service_type)),
                destination_en=str(rec.get("dest_en", "")),
                destination_tc=str(rec.get("dest_tc", "")),
                eta_iso=eta_iso,
                minutes_until=_minutes_until(eta_iso),
                remark_en=rec.get("rmk_en") or None,
            )
        )
    etas.sort(key=lambda e: (e.minutes_until is None, e.minutes_until or 999))

    return KMBRouteEtaResult(
        stop_id=stop.stop_id,
        stop_name_en=stop.name_en,
        stop_name_tc=stop.name_tc,
        route=args.route,
        service_type=args.service_type,
        etas=etas,
    )


KMB_ETA_BY_ROUTE_STOP_TOOL: ToolSpec[KMBEtaByRouteStopArgs, KMBRouteEtaResult] = ToolSpec(
    name="transport.get_kmb_eta_by_route_stop",
    description_en=(
        "ETA for a single KMB / LWB route at a single stop. Input: route number "
        "(e.g. '1A'), stop identifier (id or fuzzy name). Use when the user names "
        "a specific bus route. Do NOT use for Citybus routes — they have their "
        "own tool (transport.get_citybus_eta_by_route_stop)."
    ),
    args_schema=KMBEtaByRouteStopArgs,
    result_schema=KMBRouteEtaResult,
    handler=_route_stop_handler,
    ttl_seconds=30,
    budget_ms=2000,
    upstream_langs=frozenset({"en", "zh-Hant", "zh-Hans"}),
    upstream="data.etabus.gov.hk/kmb",
    scope=ToolScope.SPECIALIZED,
    domain="kmb_lwb_bus_only",
)
