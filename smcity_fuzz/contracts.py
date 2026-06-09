"""Per-dataset success contracts — the semantic judge for coverage results.

The old judge was `expected_tools & fired_tools != ∅`. That was a string
intersection on a set hardcoded in the catalog. It scored an agent that
only ran `geo.address_lookup` (no POIs returned) the same as one that ran
the full chain.

Each contract here declares **what success means for one dataset_id** in
terms of:
- which tools fired (the trace),
- the args those tools were called with,
- the reply text the user actually got,
- the row's transport-level status (timeout / http_error / ok).

Contracts return a `Verdict(bucket, reason)`. Buckets are semantic:

  complete            — the chain ran end-to-end and the reply has real data
  partial_chain       — the right starting tool ran but the chain didn't finish
  wrong_tool          — the agent picked an unrelated tool
  no_tool             — the agent answered from general knowledge
  empty_reply         — the agent returned text == ""
  geocoder_collision  — the collision guard fired
  error_status        — agent returned status != "ok"
  http_error / timeout / network_error — transport failures

`coverage_report.py` consumes `evaluate(row)` and renders the buckets.
This file is also the single point you edit when you rename a tool or add
a new dataset — no parallel catalog list to keep in sync.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from smcity.tools.osm_pois import POI_CATEGORIES, POI_TOOL

# --- public types ---------------------------------------------------------


@dataclass(slots=True, frozen=True)
class Verdict:
    bucket: str
    reason: str = ""


ContractFn = Callable[[dict[str, Any]], Verdict]


# --- bucket labels (used by the report renderer) --------------------------

BUCKET_LABELS: dict[str, str] = {
    "complete": "complete — chain finished with real data",
    "partial_chain": "partial — chain started but didn't deliver data",
    "wrong_tool": "wrong tool fired",
    "no_tool": "no tool fired (answered from general knowledge)",
    "empty_reply": "empty reply",
    "geocoder_collision": "geocoder collision (origin == destination)",
    "error_status": "agent returned status != ok",
    "http_error": "HTTP non-200 from /turn",
    "timeout": "request timed out",
    "network_error": "network / connection failure",
    "unknown_dataset": "no contract registered for this dataset",
}

# Which buckets count as "the agent did the right thing".
OK_BUCKETS: frozenset[str] = frozenset({"complete"})

# Which buckets indicate work to do on the agent side (vs. judge / harness).
AGENT_FAILURE_BUCKETS: frozenset[str] = frozenset(
    {"partial_chain", "wrong_tool", "no_tool", "empty_reply", "geocoder_collision"}
)


# --- helpers --------------------------------------------------------------


def _fired_tool_names(row: dict[str, Any]) -> list[str]:
    return [
        t["name"] for t in (row.get("tool_trace") or []) if isinstance(t, dict) and t.get("name")
    ]


def _ok_tool_names(row: dict[str, Any]) -> list[str]:
    """Tool calls that actually returned status=ok, not just attempts."""
    return [
        t["name"]
        for t in (row.get("tool_trace") or [])
        if isinstance(t, dict) and t.get("name") and t.get("status") == "ok"
    ]


# Light-weight heuristic: a reply that contains POI data should mention a
# place name, an address fragment, or coordinates. Anything is fine — what
# we're really checking is "did the LLM include the tool output", because
# a reply like "I couldn't find any" is a different shape.
_POI_REPLY_SIGNAL = re.compile(
    r"""
    (\d{1,3}\.\d{2,}\s*[,，]\s*\d{1,3}\.\d{2,})  # lat,lng pair
    | 7-?Eleven | Circle\s?K | VanGO | OK\s?便利店
    | (Park|Road|Street|Avenue|Plaza|Centre|Mall|Estate|House|Building|Garden|Bay|Pier|Square)
    | (路|街|道|大廈|花園|廣場|中心|商場|邨|苑|閣|園|站|公園|診所|樓)
    | (附近有|位於|located\s+at|at\s+\w+|near|located\s+near|on\s+\w+)
    """,
    re.IGNORECASE | re.VERBOSE,
)

_NEGATIVE_REPLY = re.compile(
    r"(can[''']?t\s+find|couldn[''']?t\s+find|no\s+results|nothing\s+nearby"
    r"|search\s+didn[''']?t|揾唔到|搵唔到|找唔到|沒有.*結果|查不到|未能找到)",
    re.IGNORECASE,
)


def _reply_has_poi_data(reply: str) -> bool:
    if not reply:
        return False
    if _NEGATIVE_REPLY.search(reply):
        return False
    return bool(_POI_REPLY_SIGNAL.search(reply))


def _reply_has_numeric_signal(reply: str) -> bool:
    """For transport ETAs / weather / AQHI — the reply should contain numbers."""
    if not reply:
        return False
    if _NEGATIVE_REPLY.search(reply):
        return False
    # ETA minutes, headway minutes, temperature, humidity %, AQHI band number
    return bool(re.search(r"\d", reply))


# --- contract factories ---------------------------------------------------


def _poi_chain_contract(expected_category: str) -> ContractFn:
    """Success = `geo.find_poi` fired with `category=<expected_category>`
    AND the reply has POI data.

    Bucketing:
    - `geo.find_poi(category=expected)` ok + reply has data → complete
    - same tool fired with the WRONG category → wrong_tool (right family,
      wrong slug — the v0.5 collapse-era equivalent of firing the wrong
      `geo.find_*` tool)
    - geo.find_poi attempted but errored → partial_chain
    - only geo.address_lookup fired → partial_chain
    - some unrelated tool fired → wrong_tool
    - nothing fired → no_tool
    """

    def check(row: dict[str, Any]) -> Verdict:
        fired_ok = _ok_tool_names(row)
        fired_all = _fired_tool_names(row)
        reply = (row.get("reply_text") or "").strip()
        # Find the category arg on any geo.find_poi entry in the trace.
        poi_categories: list[str] = []
        for entry in row.get("tool_trace") or []:
            if not isinstance(entry, dict):
                continue
            if entry.get("name") != POI_TOOL:
                continue
            args = entry.get("args") or {}
            cat = args.get("category") if isinstance(args, dict) else None
            if isinstance(cat, str):
                poi_categories.append(cat)

        if POI_TOOL in fired_ok and expected_category in poi_categories:
            if _reply_has_poi_data(reply):
                return Verdict(
                    "complete",
                    f"{POI_TOOL}(category={expected_category!r}) fired, reply has POI data",
                )
            return Verdict(
                "partial_chain",
                f"{POI_TOOL}(category={expected_category!r}) fired but reply lacks POI data",
            )

        if POI_TOOL in fired_ok and poi_categories:
            # Right tool, wrong category — the v0.6.0 equivalent of firing
            # `geo.find_bench` for a dentist question. Surface as wrong_tool.
            return Verdict(
                "wrong_tool",
                f"{POI_TOOL} fired with category={poi_categories!r} instead of "
                f"{expected_category!r}",
            )

        if POI_TOOL in fired_all:
            return Verdict("partial_chain", f"{POI_TOOL} attempted but did not return ok")

        if "geo.address_lookup" in fired_all:
            return Verdict("partial_chain", "address_lookup ran but geo.find_poi was never called")

        # Wrong-tool vs no-tool.
        if not fired_all:
            return Verdict("no_tool", "no tools fired — agent answered from general knowledge")

        return Verdict("wrong_tool", f"fired {fired_all} on POI question")

    return check


def _any_of_contract(*, accept: tuple[str, ...], signal: Callable[[str], bool]) -> ContractFn:
    """Success = at least one of `accept` fired (status=ok) and the reply has the expected signal.

    Used for transport ETAs (any of KMB/Citybus/GMB), weather + warnings,
    AQHI, etc. — datasets where any of several tools is an acceptable
    answer and the judging signal is "reply contains numbers".
    """

    def check(row: dict[str, Any]) -> Verdict:
        fired_ok = _ok_tool_names(row)
        fired_all = _fired_tool_names(row)
        reply = (row.get("reply_text") or "").strip()

        hit_ok = [t for t in accept if t in fired_ok]
        hit_attempted = [t for t in accept if t in fired_all]

        if hit_ok:
            if signal(reply):
                return Verdict("complete", f"{hit_ok} fired, reply has signal")
            return Verdict("partial_chain", f"{hit_ok} fired but reply lacks signal")

        if hit_attempted:
            return Verdict("partial_chain", f"{hit_attempted} attempted but did not return ok")

        if not fired_all:
            return Verdict("no_tool", "no tools fired")

        return Verdict("wrong_tool", f"fired {fired_all} instead of any of {list(accept)}")

    return check


def _single_tool_contract(tool: str, *, signal: Callable[[str], bool]) -> ContractFn:
    return _any_of_contract(accept=(tool,), signal=signal)


def _no_contract(reason: str) -> ContractFn:
    """For datasets that have no tools wired yet (e.g. S506 ferry).

    Any reply is acceptable as long as it isn't an error. The agent
    shouldn't be calling tools here — there are none — and should fall
    back to a polite "not supported yet" / general knowledge answer.
    """

    def check(row: dict[str, Any]) -> Verdict:
        reply = (row.get("reply_text") or "").strip()
        if not reply:
            return Verdict("empty_reply", reason)
        return Verdict("complete", f"no tools wired — agent answered conversationally ({reason})")

    return check


# --- transport-style signal -----------------------------------------------


def _eta_signal(reply: str) -> bool:
    # ETA replies should mention minutes or station/route numbers.
    if not reply or _NEGATIVE_REPLY.search(reply):
        return False
    return bool(
        re.search(r"(\d+\s*(min|分鐘|分|mins?)|route\s*\d+|路線?\s*\d+)", reply, re.IGNORECASE)
    )


def _journey_signal(reply: str) -> bool:
    # Journey planning — should name a station/line or a walking time.
    if not reply or _NEGATIVE_REPLY.search(reply):
        return False
    return bool(
        re.search(
            r"(MTR|港鐵|地鐵|station|站|line|綫|線|walk|步行|行|exit|出口|"
            r"\d+\s*(min|分鐘|分|mins?))",
            reply,
            re.IGNORECASE,
        )
    )


def _weather_signal(reply: str) -> bool:
    if not reply or _NEGATIVE_REPLY.search(reply):
        return False
    return bool(re.search(r"\d+\s*(°C|度|%|度C|degrees?)", reply, re.IGNORECASE))


def _aqhi_signal(reply: str) -> bool:
    if not reply or _NEGATIVE_REPLY.search(reply):
        return False
    return bool(re.search(r"(AQHI|空氣質素|空气质素|\b\d{1,2}\b)", reply))


def _facility_signal(reply: str) -> bool:
    if not reply or _NEGATIVE_REPLY.search(reply):
        return False
    return bool(
        re.search(r"(court|球場|pool|泳池|venue|場館|free|免費|booking|預訂)", reply, re.IGNORECASE)
    )


def _housing_signal(reply: str) -> bool:
    if not reply or _NEGATIVE_REPLY.search(reply):
        return False
    return bool(
        re.search(r"(estate|邨|苑|HKHA|公屋|居屋|district|區|address)", reply, re.IGNORECASE)
    )


def _address_signal(reply: str) -> bool:
    if not reply or _NEGATIVE_REPLY.search(reply):
        return False
    return bool(_POI_REPLY_SIGNAL.search(reply) or re.search(r"\d{1,3}\.\d{2,}", reply))


# --- contract registry ----------------------------------------------------


# Transport datasets (catalog dataset_ids)
_TRANSPORT_ETA_TOOLS = (
    "transport.get_kmb_eta_by_stop",
    "transport.get_kmb_eta_by_route_stop",
    "transport.get_citybus_eta_by_route_stop",
    "transport.get_gmb_eta",
)
_JOURNEY_TOOLS = (
    "transport.plan_journey",
    "transport.plan_simple_route",
    "transport.plan_walking_route",
    "transport.get_mtr_next_trains",
    "transport.plan_multimodal_journey",
    "transport.find_stops_near_point",
    "transport.find_stops_by_name",
)

CONTRACTS: dict[str, ContractFn] = {
    # Transport
    "S500": _any_of_contract(accept=_TRANSPORT_ETA_TOOLS, signal=_eta_signal),
    "S505": _any_of_contract(accept=_JOURNEY_TOOLS, signal=_journey_signal),
    "S506": _no_contract("Licensed Ferry — no public real-time API; agent should explain"),
    "S507": _any_of_contract(accept=_JOURNEY_TOOLS, signal=_journey_signal),
    "S512": _single_tool_contract("csdi.query_features", signal=_journey_signal),
    # Additional integrations (auto-id slug pattern is "X-<first20chars-of-title>")
    "X-MTR_Real-time_Next_T": _single_tool_contract(
        "transport.get_mtr_next_trains", signal=_eta_signal
    ),
    "X-KMB_/_LWB_Bus_Real-t": _any_of_contract(
        accept=(
            "transport.get_kmb_eta_by_stop",
            "transport.get_kmb_eta_by_route_stop",
            "transport.find_stops_by_name",
            "transport.find_stops_near_point",
        ),
        signal=_eta_signal,
    ),
    "X-Citybus_Real-time_ET": _any_of_contract(
        accept=("transport.get_citybus_eta_by_route_stop", "transport.get_citybus_route_stops"),
        signal=_eta_signal,
    ),
    "X-GMB_(Green_Minibus)_": _single_tool_contract("transport.get_gmb_eta", signal=_eta_signal),
    "X-OpenTripPlanner_2_Mu": _single_tool_contract(
        "transport.plan_multimodal_journey", signal=_journey_signal
    ),
    "X-Address_Lookup_(ALS)": _single_tool_contract("geo.address_lookup", signal=_address_signal),
    "X-OSM_Nominatim_(HK_vi": _no_contract("Nominatim is internal fallback only, no public tool"),
    "X-Current_Weather_+_9-": _any_of_contract(
        accept=(
            "context.get_current_weather",
            "context.get_active_warnings",
            "context.get_9day_forecast",
        ),
        signal=_weather_signal,
    ),
    "X-Air_Quality_Health_I": _single_tool_contract("context.get_aqhi", signal=_aqhi_signal),
    "X-LCSD_Basketball_Cour": _any_of_contract(
        accept=("facility.find_nearby_courts", "facility.find_nearby_pools"),
        signal=_facility_signal,
    ),
    "X-HKHA_Public_Housing_": _any_of_contract(
        accept=("housing.get_estate_info", "housing.list_estates_in_district"),
        signal=_housing_signal,
    ),
    "X-Generic_CSDI_Feature": _single_tool_contract("csdi.query_features", signal=_address_signal),
    "X-Meta_/_session_contr": _any_of_contract(
        accept=("meta.ask_user", "meta.forget_me", "meta.what_languages_are_supported"),
        signal=bool,
    ),
}

# Auto-generate POI dataset contracts S514..S549 from the POI_TOOL_NAME map
# and the catalog's id↔osm_category linkage. This is the "great-logic" payoff
# of Fix 1 + Fix 2 — one factory call per category, no copy-paste.
_CATALOG_OSM_CATEGORY_BY_ID: dict[str, str] = {
    "S514": "convenience_store",
    "S515": "supermarket",
    "S516": "public_toilet",
    "S517": "place_of_worship",
    "S518": "mtr_station_entrance",
    "S519": "recycling_location",
    "S520": "veterinarian",
    "S521": "hardware_store",
    "S522": "public_elevator",
    "S523": "hairdresser",
    "S524": "clothes_shop",
    "S525": "electronics_shop",
    "S526": "department_store",
    "S527": "variety_store",
    "S528": "houseware_shop",
    "S529": "beauty_shop",
    "S530": "optician",
    "S531": "shoe_shop",
    "S532": "greengrocer",
    "S533": "marketplace",
    "S534": "bookstore",
    "S535": "drinking_water",
    "S536": "laundry",
    "S537": "government_office",
    "S538": "kiosk",
    "S541": "dentist",
    "S542": "bookmaker",
    "S543": "bench",
    "S544": "shelter",
    "S549": "handrail",
}
for _ds_id, _osm_cat in _CATALOG_OSM_CATEGORY_BY_ID.items():
    if _osm_cat not in POI_CATEGORIES:  # pragma: no cover — startup invariant
        raise RuntimeError(
            f"fuzz catalog references unknown POI category {_osm_cat!r} for {_ds_id}"
        )
    CONTRACTS[_ds_id] = _poi_chain_contract(_osm_cat)


# --- public entry point ---------------------------------------------------


def evaluate(row: dict[str, Any]) -> Verdict:
    """Score one result row against its dataset's contract.

    Transport-level failures (timeout, http_error, network_error,
    error_status) are surfaced as their own buckets before contract
    evaluation — they're not the agent's semantic fault.
    """
    status = row.get("status")
    if status == "timeout":
        return Verdict("timeout", row.get("error") or "")
    if status == "http_error":
        return Verdict("http_error", row.get("error") or "")
    if status == "network_error":
        return Verdict("network_error", row.get("error") or "")
    if status and status != "ok":
        return Verdict("error_status", str(status))

    reply = (row.get("reply_text") or "").strip()
    if not reply:
        return Verdict("empty_reply", "")
    if "resolved to nearly the same" in reply:
        return Verdict("geocoder_collision", "")

    ds_id = row.get("expected_dataset_id")
    contract = CONTRACTS.get(ds_id or "")
    if contract is None:
        return Verdict("unknown_dataset", f"no contract for {ds_id!r}")
    return contract(row)


__all__ = [
    "AGENT_FAILURE_BUCKETS",
    "BUCKET_LABELS",
    "CONTRACTS",
    "OK_BUCKETS",
    "Verdict",
    "evaluate",
]
