"""Cross-operator stop search tools.

- transport.find_stops_near_point — k-NN over KMB stop catalog + MTR stations
- transport.find_stops_by_name    — fuzzy stop-name search across KMB + MTR

Citybus is excluded from the proximity index for now (no list-all-stops API).
"""

from __future__ import annotations

from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from rapidfuzz import fuzz, process

from smcity.geometry import haversine_km as _haversine_km
from smcity.tools.registry import ToolContext, ToolSpec
from smcity.tools.transport import _load_stations
from smcity.tools.transport_kmb import KMBStop, kmb_catalog

# --- helpers -------------------------------------------------------------


# MTR station code → (lat, lng). Covers all 93 unique stations in the HK
# heavy-rail network as of 2026-04 (matches the catalog in
# data/mtr_stations.json). Used by the journey planner (nearest-station
# search), the simple-modes geocoder (exact MTR match path), and
# find_stops_near_point. Pre-v0.4.12 this table had only 32 entries —
# stations on DRL (Disneyland), most of TML, all of TKL east of TKO, SIL,
# and the EAL beyond Tai Po simply could not be routed to, because
# `_nearest_mtr_station` returns ``None`` for any (lat, lng) outside the
# convex hull of the listed stations.
#
# Coordinates verified against OSM 2026-04 and the MTR system map; precision
# is 5 decimal places (~1 m at HK latitude) — more than enough for the
# nearest-station + walking-leg estimates.
MTR_STATION_COORDS: dict[str, tuple[float, float]] = {
    # --- Island Line (ISL) -----------------------------------------------
    "KET": (22.28140, 114.12890),  # Kennedy Town
    "HKU": (22.28380, 114.13500),  # HKU
    "SYP": (22.28540, 114.14250),  # Sai Ying Pun
    "SHW": (22.28630, 114.15150),  # Sheung Wan
    "CEN": (22.28200, 114.15820),  # Central
    "ADM": (22.27970, 114.16480),  # Admiralty
    "WAC": (22.27750, 114.17300),  # Wan Chai
    "CAB": (22.28020, 114.18490),  # Causeway Bay
    "TIH": (22.28258, 114.19160),  # Tin Hau
    "FOH": (22.28840, 114.19370),  # Fortress Hill
    "NOP": (22.29130, 114.20070),  # North Point
    "QUB": (22.28840, 114.20980),  # Quarry Bay
    "TAK": (22.28482, 114.21610),  # Tai Koo
    "SWH": (22.28200, 114.22220),  # Sai Wan Ho
    "SKW": (22.27900, 114.22930),  # Shau Kei Wan
    "HFC": (22.27660, 114.24010),  # Heng Fa Chuen
    "CHW": (22.26470, 114.23700),  # Chai Wan
    # --- Tsuen Wan Line (TWL) --------------------------------------------
    "TSW": (22.37370, 114.11750),  # Tsuen Wan
    "TWH": (22.37020, 114.12520),  # Tai Wo Hau
    "KWH": (22.36310, 114.13140),  # Kwai Hing
    "KWF": (22.35730, 114.12800),  # Kwai Fong
    "LAK": (22.34860, 114.12620),  # Lai King
    "MEF": (22.33750, 114.13780),  # Mei Foo
    "LCK": (22.33700, 114.14810),  # Lai Chi Kok
    "CSW": (22.33570, 114.15620),  # Cheung Sha Wan
    "SSP": (22.33100, 114.16220),  # Sham Shui Po
    "PRE": (22.32470, 114.16850),  # Prince Edward
    "MOK": (22.31950, 114.16920),  # Mong Kok
    "YMT": (22.31310, 114.17080),  # Yau Ma Tei
    "JOR": (22.30500, 114.17170),  # Jordan
    "TST": (22.29790, 114.17220),  # Tsim Sha Tsui
    # --- Kwun Tong Line (KTL) --------------------------------------------
    "TIK": (22.30430, 114.25260),  # Tiu Keng Leng
    "YAT": (22.29780, 114.23700),  # Yau Tong
    "LAT": (22.30680, 114.23260),  # Lam Tin
    "KWT": (22.31210, 114.22610),  # Kwun Tong
    "NTK": (22.31570, 114.21860),  # Ngau Tau Kok
    "KOB": (22.32290, 114.21430),  # Kowloon Bay
    "CHH": (22.33480, 114.20870),  # Choi Hung
    "DIH": (22.34000, 114.20130),  # Diamond Hill
    "WTS": (22.34160, 114.19370),  # Wong Tai Sin
    "LOF": (22.33800, 114.18700),  # Lok Fu
    "KOT": (22.33710, 114.17610),  # Kowloon Tong
    "SKM": (22.33200, 114.16890),  # Shek Kip Mei
    "HOM": (22.30930, 114.18270),  # Ho Man Tin
    "WKS": (22.30560, 114.18910),  # Whampoa
    # --- East Rail Line (EAL) --------------------------------------------
    "EXC": (22.28200, 114.17310),  # Exhibition Centre
    "HUH": (22.30320, 114.18160),  # Hung Hom
    "MKK": (22.32210, 114.17260),  # Mong Kok East
    "TAW": (22.37270, 114.17870),  # Tai Wai
    "SHT": (22.38170, 114.18700),  # Sha Tin
    "FOT": (22.39530, 114.19780),  # Fo Tan
    "RAC": (22.40150, 114.20310),  # Racecourse
    "UNI": (22.41490, 114.20980),  # University
    "TAP": (22.44530, 114.17020),  # Tai Po Market
    "TWO": (22.45090, 114.16140),  # Tai Wo
    "FAN": (22.49220, 114.13870),  # Fanling
    "SHS": (22.50110, 114.12830),  # Sheung Shui
    "LOW": (22.52830, 114.11310),  # Lo Wu
    "LMC": (22.51470, 114.06580),  # Lok Ma Chau
    # --- Tseung Kwan O Line (TKL) ----------------------------------------
    "POA": (22.32200, 114.25700),  # Po Lam
    "HAH": (22.31580, 114.26410),  # Hang Hau
    "TKO": (22.30760, 114.26000),  # Tseung Kwan O
    "LHP": (22.29500, 114.26930),  # LOHAS Park
    # --- South Island Line (SIL) -----------------------------------------
    "OCP": (22.24860, 114.17450),  # Ocean Park
    "WCH": (22.24790, 114.16810),  # Wong Chuk Hang
    "LET": (22.24180, 114.15650),  # Lei Tung
    "SOH": (22.24250, 114.14950),  # South Horizons
    # --- Tuen Ma Line (TML) ----------------------------------------------
    "TUM": (22.39470, 113.97350),  # Tuen Mun
    "SIH": (22.41180, 113.97860),  # Siu Hong
    "TIS": (22.44800, 114.00490),  # Tin Shui Wai
    "LOP": (22.44770, 114.02540),  # Long Ping
    "YUL": (22.44590, 114.03530),  # Yuen Long
    "KSR": (22.43510, 114.06330),  # Kam Sheung Road
    "TWW": (22.36830, 114.10950),  # Tsuen Wan West
    "NAC": (22.32660, 114.15380),  # Nam Cheong
    "AUS": (22.30450, 114.16660),  # Austin
    "ETS": (22.29540, 114.17340),  # East Tsim Sha Tsui
    "SUW": (22.32603, 114.19070),  # Sung Wong Toi
    "KAT": (22.33040, 114.19900),  # Kai Tak
    "HIK": (22.36400, 114.17190),  # Hin Keng
    "CKT": (22.37420, 114.18590),  # Che Kung Temple
    "SHM": (22.37590, 114.19480),  # Sha Tin Wai
    "CIO": (22.38260, 114.20300),  # City One
    "STW": (22.38770, 114.20860),  # Shek Mun
    # --- Tung Chung Line (TCL) -------------------------------------------
    "HOK": (22.28490, 114.15870),  # Hong Kong
    "KOW": (22.30390, 114.16140),  # Kowloon
    "OLY": (22.31740, 114.16020),  # Olympic
    "TSY": (22.35820, 114.10750),  # Tsing Yi
    "SUN": (22.33220, 114.02890),  # Sunny Bay
    "TUC": (22.28910, 113.94150),  # Tung Chung
    # --- Airport Express (AEL) -------------------------------------------
    "AIR": (22.31570, 113.93690),  # Airport
    "AWE": (22.32160, 113.94080),  # AsiaWorld-Expo
    # --- Disneyland Resort Line (DRL) ------------------------------------
    "DIS": (22.31480, 114.04460),  # Disneyland Resort
}


# --- find_stops_near_point -----------------------------------------------


class FindStopsNearPointArgs(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    radius_m: int = Field(
        default=500, ge=50, le=5000, description="Search radius in metres (default 500 m)."
    )
    max_results: int = Field(default=8, ge=1, le=30)
    operators: list[str] = Field(
        default_factory=lambda: ["kmb", "mtr"],
        description="Which operators to include: 'kmb', 'mtr'. Citybus omitted in v0.1.",
    )


class NearbyStop(BaseModel):
    operator: str  # "kmb" | "mtr"
    id: str
    name_en: str
    name_tc: str | None = None
    lat: float
    lng: float
    distance_m: int


class FindStopsNearPointResult(BaseModel):
    query_lat: float
    query_lng: float
    stops: list[NearbyStop]
    source: str = "kmb+mtr (local)"


async def _near_handler(args: FindStopsNearPointArgs, ctx: ToolContext) -> FindStopsNearPointResult:
    ops = {op.lower() for op in args.operators}
    candidates: list[NearbyStop] = []
    radius_km = args.radius_m / 1000.0

    if "kmb" in ops:
        stops: list[KMBStop] = await kmb_catalog().all()
        for s in stops:
            d_km = _haversine_km(args.lat, args.lng, s.lat, s.lng)
            if d_km > radius_km:
                continue
            candidates.append(
                NearbyStop(
                    operator="kmb",
                    id=s.stop_id,
                    name_en=s.name_en,
                    name_tc=s.name_tc or None,
                    lat=s.lat,
                    lng=s.lng,
                    distance_m=round(d_km * 1000),
                )
            )

    if "mtr" in ops:
        for st in _load_stations():
            coords = MTR_STATION_COORDS.get(st.code)
            if not coords:
                continue
            lat, lng = coords
            d_km = _haversine_km(args.lat, args.lng, lat, lng)
            if d_km > radius_km:
                continue
            candidates.append(
                NearbyStop(
                    operator="mtr",
                    id=st.code,
                    name_en=st.names.get("en", ""),
                    name_tc=st.names.get("zh-Hant") or None,
                    lat=lat,
                    lng=lng,
                    distance_m=round(d_km * 1000),
                )
            )

    candidates.sort(key=lambda c: c.distance_m)
    return FindStopsNearPointResult(
        query_lat=args.lat,
        query_lng=args.lng,
        stops=candidates[: args.max_results],
    )


FIND_STOPS_NEAR_POINT_TOOL: ToolSpec[FindStopsNearPointArgs, FindStopsNearPointResult] = ToolSpec(
    name="transport.find_stops_near_point",
    description_en=(
        "Find KMB and MTR stops within a radius (m) of a lat/lng. Always call "
        "geo.address_lookup first to turn a place name into coordinates, then "
        "pass the resolved lat/lng here. Returns up to max_results stops sorted "
        "by distance."
    ),
    args_schema=FindStopsNearPointArgs,
    result_schema=FindStopsNearPointResult,
    handler=_near_handler,
    ttl_seconds=60 * 60,
    budget_ms=1500,
    upstream_langs=frozenset({"en", "zh-Hant", "zh-Hans"}),
    upstream="kmb+mtr",
)


# --- find_stops_by_name --------------------------------------------------


class FindStopsByNameArgs(BaseModel):
    # gpt-oss-120b emits `name` for "thing to look up" fields (v0.5.4 live
    # smoke). Accept it as an alias for `query` so the LLM's guess matches.
    model_config = ConfigDict(populate_by_name=True)

    query: str = Field(
        min_length=1,
        max_length=120,
        validation_alias=AliasChoices("query", "name", "stop_name", "q"),
    )
    operators: list[str] = Field(default_factory=lambda: ["kmb", "mtr"])
    max_results: int = Field(default=8, ge=1, le=30)


class FindStopsByNameResult(BaseModel):
    query: str
    stops: list[NearbyStop]
    source: str = "kmb+mtr (local)"


async def _by_name_handler(args: FindStopsByNameArgs, ctx: ToolContext) -> FindStopsByNameResult:
    ops = {op.lower() for op in args.operators}
    entries: list[tuple[str, NearbyStop]] = []

    if "kmb" in ops:
        for s in await kmb_catalog().all():
            for alias in (s.name_en, s.name_tc, s.name_sc):
                if alias:
                    entries.append(
                        (
                            alias,
                            NearbyStop(
                                operator="kmb",
                                id=s.stop_id,
                                name_en=s.name_en,
                                name_tc=s.name_tc or None,
                                lat=s.lat,
                                lng=s.lng,
                                distance_m=0,
                            ),
                        )
                    )
    if "mtr" in ops:
        for st in _load_stations():
            coords = MTR_STATION_COORDS.get(st.code, (0.0, 0.0))
            for alias in st.names.values():
                if alias:
                    entries.append(
                        (
                            alias,
                            NearbyStop(
                                operator="mtr",
                                id=st.code,
                                name_en=st.names.get("en", ""),
                                name_tc=st.names.get("zh-Hant") or None,
                                lat=coords[0],
                                lng=coords[1],
                                distance_m=0,
                            ),
                        )
                    )

    names_only = [e[0] for e in entries]
    matches = process.extract(
        args.query, names_only, scorer=fuzz.WRatio, limit=args.max_results * 3
    )
    seen: set[tuple[str, str]] = set()
    out: list[NearbyStop] = []
    for _, _score, idx in matches:
        stop = entries[idx][1]
        key = (stop.operator, stop.id)
        if key in seen:
            continue
        seen.add(key)
        out.append(stop)
        if len(out) >= args.max_results:
            break
    return FindStopsByNameResult(query=args.query, stops=out)


FIND_STOPS_BY_NAME_TOOL: ToolSpec[FindStopsByNameArgs, FindStopsByNameResult] = ToolSpec(
    name="transport.find_stops_by_name",
    description_en=(
        "Fuzzy-search KMB stops and MTR stations by name (EN / 繁體 / 简体). Returns "
        "the top candidates across both operators — use when the user names a place "
        "but no coordinates are known yet."
    ),
    args_schema=FindStopsByNameArgs,
    result_schema=FindStopsByNameResult,
    handler=_by_name_handler,
    ttl_seconds=60 * 60,
    budget_ms=800,
    upstream_langs=frozenset({"en", "zh-Hant", "zh-Hans"}),
    upstream="kmb+mtr",
)
