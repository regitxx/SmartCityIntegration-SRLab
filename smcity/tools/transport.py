"""Transport tools.

Phase 1a ships:
- transport.get_mtr_next_trains  (MTR Next Train API)

Lookups (find-by-name / find-near-point) are embedded in the resolver helper
for now; they'll be split into their own tools in Phase 1b.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

import httpx
from pydantic import BaseModel, Field
from rapidfuzz import fuzz, process

from smcity.tools.registry import ToolContext, ToolSpec, ToolUpstreamError

_DATA_ROOT = Path(__file__).resolve().parent.parent.parent / "data"
MTR_NEXT_TRAIN_URL = "https://rt.data.gov.hk/v1/transport/mtr/getSchedule.php"


class MTRStation(BaseModel):
    code: str
    line: str
    names: dict[str, str]


@cache
def _load_stations() -> list[MTRStation]:
    raw = json.loads((_DATA_ROOT / "mtr_stations.json").read_text(encoding="utf-8"))
    return [MTRStation.model_validate(r) for r in raw]


def resolve_mtr_station(name: str) -> MTRStation | None:
    """Match a station name (EN / 繁體 / 简体) to a single station.

    Two-pass strategy:

      1. **Exact match** (case-insensitive) against every language variant —
         deterministic, sub-millisecond, handles the 99% case of the LLM
         passing a real station name verbatim.
      2. **`token_sort_ratio` fuzzy match** at cutoff 85 — tolerant of
         word reordering and minor typos but, crucially, NOT of substring
         matches. The previous `fuzz.WRatio` matcher scored
         ``"Polytechnic University Hong Kong"`` against the MTR
         ``"University"`` station at 90 because of `partial_ratio` weighting,
         producing the v0.4.11 PolyU = CityU = (22.4149, 114.2098) bug.
         `token_sort_ratio` scores that pair at 47 (well below cutoff) while
         keeping legitimate near-matches like ``"kowloon tong"`` → ``"Kowloon
         Tong"`` at 83.
    """
    stations = _load_stations()
    candidates: list[tuple[str, MTRStation]] = []
    for st in stations:
        for n in st.names.values():
            candidates.append((n, st))
            candidates.append((n.lower(), st))

    target = name.strip().casefold()
    for label, station in candidates:
        if label.strip().casefold() == target:
            return station

    names_only = [c[0] for c in candidates]
    match = process.extractOne(
        name, names_only, scorer=fuzz.token_sort_ratio, score_cutoff=85
    )
    if match is None:
        return None
    return candidates[match[2]][1]


# --- tool definition ------------------------------------------------------


class MTRNextTrainsArgs(BaseModel):
    station_name: str = Field(
        min_length=1,
        description="Name of the MTR station (EN / 繁體 / 简体). Fuzzy matched.",
    )
    # Optional line hint — helps when a station is on multiple lines (e.g. Admiralty).
    line: str | None = Field(
        default=None,
        description="Optional MTR line code (ISL, TWL, KTL, EAL, TML, TKL, SIL, TCL, AEL, DRL).",
    )


class MTRNextTrain(BaseModel):
    direction: str  # "UP" or "DOWN"
    destination_code: str
    time: str  # minutes until arrival as advertised by MTR
    platform: str | None = None
    sequence: int


class MTRNextTrainsResult(BaseModel):
    station_code: str
    line: str
    station_name_en: str
    station_name_tc: str
    next_trains: list[MTRNextTrain]
    system_status: str  # "1" normal, "0" disrupted/alert per API
    message: str | None = None
    source: str = "rt.data.gov.hk/mtr"


async def _handler(args: MTRNextTrainsArgs, ctx: ToolContext) -> MTRNextTrainsResult:
    station = resolve_mtr_station(args.station_name)
    if station is None:
        raise ToolUpstreamError(f"MTR station not found: {args.station_name!r}")
    line = args.line or station.line

    params = {"line": line, "sta": station.code, "lang": "EN" if ctx.query_lang == "en" else "TC"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as h:
            r = await h.get(MTR_NEXT_TRAIN_URL, params=params)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as err:
        raise ToolUpstreamError(f"MTR request failed: {err}") from err

    status = str(data.get("status", "0"))
    message = data.get("message")
    trains: list[MTRNextTrain] = []

    # MTR payload schema: data[f"{line}-{code}"] -> { "UP": [...], "DOWN": [...], ... }
    block = (data.get("data") or {}).get(f"{line}-{station.code}") or {}
    for direction in ("UP", "DOWN"):
        for item in block.get(direction, []) or []:
            trains.append(
                MTRNextTrain(
                    direction=direction,
                    destination_code=str(item.get("dest", "")),
                    time=str(item.get("ttnt", item.get("time", ""))),
                    platform=str(item.get("plat")) if item.get("plat") is not None else None,
                    sequence=int(item.get("seq", 0)),
                )
            )

    return MTRNextTrainsResult(
        station_code=station.code,
        line=line,
        station_name_en=station.names.get("en", ""),
        station_name_tc=station.names.get("zh-Hant", ""),
        next_trains=trains,
        system_status=status,
        message=message if isinstance(message, str) else None,
    )


MTR_NEXT_TRAINS_TOOL: ToolSpec[MTRNextTrainsArgs, MTRNextTrainsResult] = ToolSpec(
    name="transport.get_mtr_next_trains",
    description_en=(
        "Next trains for an MTR station. Input: station name in EN / 繁體 / 简体 "
        "(fuzzy-matched; e.g. 'Central', '中環', '中环', 'Sheung Wan'). Returns up to "
        "the next 4 trains per direction plus service-status flag."
    ),
    args_schema=MTRNextTrainsArgs,
    result_schema=MTRNextTrainsResult,
    handler=_handler,
    ttl_seconds=10,
    budget_ms=1500,
    upstream_langs=frozenset({"en", "zh-Hant"}),
    upstream="rt.data.gov.hk/mtr",
)
