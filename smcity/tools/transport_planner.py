"""transport.plan_simple_route — hand-rolled walk → MTR → walk journey planner.

Phase 1b-style simple planner for the hero scenario "Sheung Wan → Sha Tin":

1. Resolve origin + destination to (lat, lng) — via ALS if given as names.
2. Find the nearest MTR station within walking distance of each.
3. BFS over the station graph to find the shortest station-sequence + the
   line for each edge. Inter-station runtime ~= 2 min; interchange ~= 5 min.
4. Compose legs: [walk → board line A → (optional transfer → line B → …) →
   walk] with duration estimates.

This does NOT replace OpenTripPlanner 2 — it's the quick v0.1 multimodal
answer for queries dominated by MTR routing. Buses, minibuses, and door-to-
door routing come with OTP2 in the next phase.
"""

from __future__ import annotations

import heapq
import json
from dataclasses import dataclass
from functools import cache
from itertools import pairwise
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from smcity.geometry import haversine_m as _haversine_m
from smcity.tools.registry import ToolContext, ToolScope, ToolSpec, ToolUpstreamError
from smcity.tools.transport import _load_stations as load_mtr_stations
from smcity.tools.transport_search import MTR_STATION_COORDS

_DATA_ROOT = Path(__file__).resolve().parent.parent.parent / "data"

# Runtime tuneables — deliberately conservative defaults.
_WALK_SPEED_MPS = 1.3
_INTERSTATION_MIN = 2
_INTERCHANGE_PENALTY_MIN = 5
_MAX_WALK_TO_MTR_M = 1500  # max acceptable walk from origin/dest to an MTR station


# --- topology loader -----------------------------------------------------


@dataclass(slots=True)
class _Topology:
    adjacency: dict[str, set[tuple[str, str]]]  # station -> {(neighbour, line_code), ...}
    lines_of: dict[str, set[str]]  # station -> {line_code, ...}
    line_names: dict[str, dict[str, str]]  # line_code -> {name_en, name_tc}


@cache
def _topology() -> _Topology:
    raw = json.loads((_DATA_ROOT / "mtr_lines.json").read_text(encoding="utf-8"))
    adjacency: dict[str, set[tuple[str, str]]] = {}
    lines_of: dict[str, set[str]] = {}
    line_names: dict[str, dict[str, str]] = {}

    def _add_edge(a: str, b: str, line: str) -> None:
        adjacency.setdefault(a, set()).add((b, line))
        adjacency.setdefault(b, set()).add((a, line))

    for line_code, line in raw["lines"].items():
        line_names[line_code] = {
            "name_en": line.get("name_en", line_code),
            "name_tc": line.get("name_tc", line_code),
        }
        stations: list[str] = line["stations"]
        for a, b in pairwise(stations):
            _add_edge(a, b, line_code)
        for st in stations:
            lines_of.setdefault(st, set()).add(line_code)
        for _branch_code, branch_stops in (line.get("branches") or {}).items():
            for a, b in pairwise(branch_stops):
                _add_edge(a, b, line_code)
            for st in branch_stops:
                lines_of.setdefault(st, set()).add(line_code)
    return _Topology(adjacency=adjacency, lines_of=lines_of, line_names=line_names)


# --- graph helpers -------------------------------------------------------


def _nearest_mtr_station(
    lat: float, lng: float, *, max_m: float = _MAX_WALK_TO_MTR_M
) -> tuple[str, float] | None:
    best_code: str | None = None
    best_m = max_m
    for code, (s_lat, s_lng) in MTR_STATION_COORDS.items():
        d = _haversine_m(lat, lng, s_lat, s_lng)
        if d < best_m:
            best_m = d
            best_code = code
    if best_code is None:
        return None
    return best_code, best_m


def _shortest_path(src: str, dst: str) -> list[tuple[str, str | None]] | None:
    """BFS with (node, incoming_line) state so we can count interchanges.

    Returns a list of (station_code, line_taken_to_reach_here) pairs. The first
    entry has line=None (we haven't boarded yet).
    """
    topo = _topology()
    if src == dst:
        return [(src, None)]
    if src not in topo.adjacency or dst not in topo.adjacency:
        return None

    # Dijkstra with (cost, station, line_in, path-so-far) entries. Edges cost
    # _INTERSTATION_MIN + an extra _INTERCHANGE_PENALTY_MIN when the line changes.
    pq: list[tuple[int, str, str | None, list[tuple[str, str | None]]]] = [
        (0, src, None, [(src, None)])
    ]
    seen: dict[tuple[str, str | None], int] = {(src, None): 0}

    while pq:
        cost, node, line_in, path = heapq.heappop(pq)
        if node == dst:
            return path
        for neigh, line_out in topo.adjacency.get(node, set()):
            step = _INTERSTATION_MIN
            if line_in is not None and line_out != line_in:
                step += _INTERCHANGE_PENALTY_MIN
            new_cost = cost + step
            state = (neigh, line_out)
            if state in seen and seen[state] <= new_cost:
                continue
            seen[state] = new_cost
            heapq.heappush(pq, (new_cost, neigh, line_out, [*path, (neigh, line_out)]))
    return None


# --- tool schema ---------------------------------------------------------


class PlanSimpleRouteArgs(BaseModel):
    origin_lat: float | None = Field(default=None, ge=-90, le=90)
    origin_lng: float | None = Field(default=None, ge=-180, le=180)
    origin_station: str | None = Field(
        default=None,
        description="Optional MTR station code or name for the origin. Fuzzy matched.",
    )
    destination_lat: float | None = Field(default=None, ge=-90, le=90)
    destination_lng: float | None = Field(default=None, ge=-180, le=180)
    destination_station: str | None = Field(
        default=None,
        description="Optional MTR station code or name for the destination. Fuzzy matched.",
    )
    preferred_mode: Literal["mtr", "any"] = Field(default="mtr")


class RouteLeg(BaseModel):
    kind: Literal["walk", "board", "ride", "transfer", "alight"]
    from_name_en: str | None = None
    to_name_en: str | None = None
    from_name_tc: str | None = None
    to_name_tc: str | None = None
    line: str | None = None  # line_code for ride/transfer legs
    line_name_en: str | None = None
    line_name_tc: str | None = None
    distance_m: int | None = None
    duration_min: int | None = None
    stations: list[str] | None = None  # station codes traversed on a ride leg


class PlanSimpleRouteResult(BaseModel):
    ok: bool
    reason: str | None = None  # set when ok=false
    origin_station: str | None = None
    destination_station: str | None = None
    total_duration_min: int | None = None
    walk_from_origin_m: int | None = None
    walk_to_destination_m: int | None = None
    legs: list[RouteLeg]
    source: str = "smcity.planner (walk+MTR)"


# --- resolver helpers ----------------------------------------------------


def _resolve_station(hint: str | None) -> str | None:
    """Map a hint (code or fuzzy name) to a station code."""
    if not hint:
        return None
    h = hint.strip().upper()
    if h in MTR_STATION_COORDS:
        return h
    # Fall back to fuzzy name match via the static catalog.
    from rapidfuzz import fuzz, process

    rows: list[tuple[str, str]] = []
    for st in load_mtr_stations():
        for name in st.names.values():
            if name:
                rows.append((name, st.code))
                rows.append((name.lower(), st.code))
    names_only = [r[0] for r in rows]
    match = process.extractOne(hint, names_only, scorer=fuzz.WRatio, score_cutoff=78)
    if match is None:
        return None
    return rows[match[2]][1]


def _station_names(code: str) -> tuple[str, str]:
    for st in load_mtr_stations():
        if st.code == code:
            return st.names.get("en", code), st.names.get("zh-Hant", "")
    return code, ""


# --- handler -------------------------------------------------------------


async def _handler(args: PlanSimpleRouteArgs, ctx: ToolContext) -> PlanSimpleRouteResult:
    if args.preferred_mode != "mtr":
        raise ToolUpstreamError("only preferred_mode='mtr' is supported in v0.1")

    # Resolve origin end: prefer explicit station, otherwise nearest to lat/lng.
    origin_code = _resolve_station(args.origin_station)
    origin_lat = args.origin_lat
    origin_lng = args.origin_lng
    walk_from_origin_m: int | None = None
    if origin_code is None:
        if origin_lat is None or origin_lng is None:
            return PlanSimpleRouteResult(
                ok=False,
                reason="Need origin_station or (origin_lat, origin_lng).",
                legs=[],
            )
        found = _nearest_mtr_station(origin_lat, origin_lng)
        if found is None:
            return PlanSimpleRouteResult(
                ok=False,
                reason=f"No MTR station within {_MAX_WALK_TO_MTR_M} m of origin.",
                legs=[],
            )
        origin_code, m = found
        walk_from_origin_m = round(m)
    else:
        # If the caller also gave lat/lng, compute the walking leg;
        # otherwise assume the user is at the station entrance.
        if origin_lat is not None and origin_lng is not None:
            s_lat, s_lng = MTR_STATION_COORDS.get(origin_code, (0.0, 0.0))
            walk_from_origin_m = round(_haversine_m(origin_lat, origin_lng, s_lat, s_lng))

    # Resolve destination end similarly.
    dest_code = _resolve_station(args.destination_station)
    dest_lat = args.destination_lat
    dest_lng = args.destination_lng
    walk_to_destination_m: int | None = None
    if dest_code is None:
        if dest_lat is None or dest_lng is None:
            return PlanSimpleRouteResult(
                ok=False,
                reason="Need destination_station or (destination_lat, destination_lng).",
                legs=[],
            )
        found = _nearest_mtr_station(dest_lat, dest_lng)
        if found is None:
            return PlanSimpleRouteResult(
                ok=False,
                reason=f"No MTR station within {_MAX_WALK_TO_MTR_M} m of destination.",
                legs=[],
            )
        dest_code, m = found
        walk_to_destination_m = round(m)
    else:
        if dest_lat is not None and dest_lng is not None:
            s_lat, s_lng = MTR_STATION_COORDS.get(dest_code, (0.0, 0.0))
            walk_to_destination_m = round(_haversine_m(dest_lat, dest_lng, s_lat, s_lng))

    path = _shortest_path(origin_code, dest_code)
    if path is None:
        return PlanSimpleRouteResult(
            ok=False,
            reason=f"No MTR path between {origin_code} and {dest_code}.",
            legs=[],
        )

    topo = _topology()
    legs: list[RouteLeg] = []
    total_min = 0

    # Walk from origin to origin_code (if we have lat/lng).
    if walk_from_origin_m is not None and walk_from_origin_m > 30:
        walk_min = max(1, round(walk_from_origin_m / (_WALK_SPEED_MPS * 60)))
        o_en, o_tc = _station_names(origin_code)
        legs.append(
            RouteLeg(
                kind="walk",
                from_name_en="(origin)",
                to_name_en=o_en,
                to_name_tc=o_tc,
                distance_m=walk_from_origin_m,
                duration_min=walk_min,
            )
        )
        total_min += walk_min

    # Board + ride + transfer legs, aggregated by consecutive same-line runs.
    current_line: str | None = None
    run_start_idx = 0
    for i, (_station, line_in) in enumerate(path):
        if i == 0:
            continue
        if line_in != current_line:
            # emit previous run (if any)
            if current_line is not None:
                _emit_ride(legs, topo, path, run_start_idx, i - 1, current_line)
            current_line = line_in
            run_start_idx = i - 1
            o_en, o_tc = _station_names(path[run_start_idx][0])
            legs.append(
                RouteLeg(
                    kind="board",
                    from_name_en=o_en,
                    from_name_tc=o_tc,
                    line=current_line,
                    line_name_en=topo.line_names[current_line]["name_en"] if current_line else None,
                    line_name_tc=topo.line_names[current_line]["name_tc"] if current_line else None,
                )
            )
    if current_line is not None:
        _emit_ride(legs, topo, path, run_start_idx, len(path) - 1, current_line)

    # Duration accounting: count ride legs
    ride_min = 0
    transfer_min = 0
    last_line: str | None = None
    for leg in legs:
        if leg.kind == "ride" and leg.duration_min:
            ride_min += leg.duration_min
        if leg.kind == "board" and leg.line and last_line and leg.line != last_line:
            transfer_min += _INTERCHANGE_PENALTY_MIN
        if leg.line:
            last_line = leg.line
    total_min += ride_min + transfer_min

    # Alight + walk to destination.
    d_en, d_tc = _station_names(dest_code)
    legs.append(
        RouteLeg(
            kind="alight",
            to_name_en=d_en,
            to_name_tc=d_tc,
        )
    )
    if walk_to_destination_m is not None and walk_to_destination_m > 30:
        walk_min = max(1, round(walk_to_destination_m / (_WALK_SPEED_MPS * 60)))
        legs.append(
            RouteLeg(
                kind="walk",
                from_name_en=d_en,
                from_name_tc=d_tc,
                to_name_en="(destination)",
                distance_m=walk_to_destination_m,
                duration_min=walk_min,
            )
        )
        total_min += walk_min

    return PlanSimpleRouteResult(
        ok=True,
        origin_station=origin_code,
        destination_station=dest_code,
        total_duration_min=total_min,
        walk_from_origin_m=walk_from_origin_m,
        walk_to_destination_m=walk_to_destination_m,
        legs=legs,
    )


def _emit_ride(
    legs: list[RouteLeg],
    topo: _Topology,
    path: list[tuple[str, str | None]],
    start_idx: int,
    end_idx: int,
    line: str,
) -> None:
    """Append a `ride` leg covering path[start_idx..end_idx] on `line`."""
    stations = [path[i][0] for i in range(start_idx, end_idx + 1)]
    hops = max(1, end_idx - start_idx)
    from_en, from_tc = _station_names(stations[0])
    to_en, to_tc = _station_names(stations[-1])
    legs.append(
        RouteLeg(
            kind="ride",
            from_name_en=from_en,
            from_name_tc=from_tc,
            to_name_en=to_en,
            to_name_tc=to_tc,
            line=line,
            line_name_en=topo.line_names[line]["name_en"],
            line_name_tc=topo.line_names[line]["name_tc"],
            stations=stations,
            duration_min=hops * _INTERSTATION_MIN,
        )
    )


PLAN_SIMPLE_ROUTE_TOOL: ToolSpec[PlanSimpleRouteArgs, PlanSimpleRouteResult] = ToolSpec(
    name="transport.plan_simple_route",
    description_en=(
        "Plan a walk + MTR + walk journey between two points in Hong Kong. "
        "MTR-ONLY. Do not call for walking, bus, minibus, taxi, or ferry — "
        "those have their own tools or get conversational answers. "
        "PRECONDITION: the user has explicitly chosen MTR (or 地鐵 / 港鐵). "
        "If they only asked 'how do I get from X to Y?' without a mode, call "
        "transport.plan_journey instead. "
        "Args are STRICT — use the named fields (NOT generic 'origin' / "
        "'destination' / 'mode' strings). Pick ONE of: "
        "(origin_station + destination_station), or "
        "(origin_lat + origin_lng + destination_lat + destination_lng), or a "
        "mix. `preferred_mode` must be 'mtr' (the default)."
    ),
    args_schema=PlanSimpleRouteArgs,
    result_schema=PlanSimpleRouteResult,
    handler=_handler,
    ttl_seconds=0,
    budget_ms=800,
    cacheable=False,
    upstream_langs=frozenset({"en", "zh-Hant"}),
    upstream="smcity.planner",
    scope=ToolScope.SPECIALIZED,
    domain="mtr_only",
)
