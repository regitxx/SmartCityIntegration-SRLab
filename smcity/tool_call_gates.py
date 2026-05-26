# CJK punctuation in prose is intentional.
"""Pre-execution gates on the LLM's proposed tool calls.

Completes the lifecycle-stage abstraction for the orchestrator:

| Stage             | Module                       | Inputs                          |
|-------------------|------------------------------|---------------------------------|
| Pre-execution     | tool_call_gates.py (this)    | LLM's proposed tool_calls       |
| Post-execution    | chain_rules.py               | Tool results + user query       |
| Post-synthesis    | synthesis_invariants.py      | Reply text + tool results       |

Each engine runs declarative checks against its stage's data and, on a
violation, asks the orchestrator to re-prompt the LLM with a corrective
hint. None of them loop — one retry per turn per engine, then we accept
whatever the LLM produced and move on.

The first registered gate (`ASK_USER_ONLY_GATE`) catches the documented
"agent leads with `meta.ask_user` instead of trying a search tool"
failure mode. The gate looks at the SHAPE of the proposed call list
(single tool, that tool is meta.ask_user) — it doesn't care about session
history because the structural rule is the same every turn: try before
asking.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

# --- violation + gate types -----------------------------------------------


@dataclass(frozen=True, slots=True)
class GateViolation:
    """A proposed tool-call list that failed a gate.

    `corrective_prompt` is a system message the orchestrator appends to the
    message history before re-prompting the LLM. It explains why we're
    rejecting the proposal and what the LLM should do instead.
    """

    name: str  # gate name, for telemetry
    kind: str  # category slug, e.g. "ask_user_only"
    corrective_prompt: str


@dataclass(frozen=True, slots=True)
class ToolCallGate:
    """A single pre-execution check on a tool-call list."""

    name: str
    check: Callable[[list[dict[str, Any]]], GateViolation | None]


# --- engine ---------------------------------------------------------------


def apply_gates(
    proposed_calls: list[dict[str, Any]],
    gates: Iterable[ToolCallGate] | None = None,
) -> GateViolation | None:
    """Run each gate; return the first violation, or None.

    Order is registration order — earlier gates take precedence when
    multiple would match.
    """
    if not proposed_calls:
        return None  # nothing to gate
    if gates is None:
        gates = DEFAULT_GATES
    for gate in gates:
        violation = gate.check(proposed_calls)
        if violation is not None:
            return violation
    return None


# --- ask_user_only_first_move gate ----------------------------------------


def _ask_user_only_check(
    proposed: list[dict[str, Any]],
) -> GateViolation | None:
    """Fire when the LLM proposes ONLY `meta.ask_user` and no other tool.

    `meta.ask_user` is marked `[FALLBACK]` in its description for a reason:
    it should never be the agent's first move. The right move is to try
    the most plausible search/lookup tool — which will return either
    matching records, an empty list, or candidates to pick from. All three
    are more useful to the user than a clarifying question.

    If the LLM combines ask_user WITH another tool call (rare but legal),
    the gate stays silent — the other tool will do the search work.
    """
    if len(proposed) != 1:
        return None
    if proposed[0].get("name") != "meta.ask_user":
        return None
    return GateViolation(
        name="ask_user_only_first_move",
        kind="ask_user_only",
        corrective_prompt=(
            "You proposed `meta.ask_user` as the ONLY tool call. That tool "
            "is marked `[FALLBACK]` and is reserved for cases where you "
            "have already tried a search/lookup tool this turn and it "
            "returned ambiguous results. Try again: pick the most plausible "
            "search or lookup tool for the user's query and call it now "
            "(e.g., transport.plan_journey for 'how do I get from X to Y?', "
            "geo.address_lookup for landmark queries, geo.find_poi with a "
            "`category` slug for nearest-X queries). The search tool's "
            "response — even "
            "an empty list — is more useful than a clarifying question. "
            "Only fall back to meta.ask_user if no search tool fits."
        ),
    )


ASK_USER_ONLY_GATE = ToolCallGate(
    name="ask_user_only_first_move",
    check=_ask_user_only_check,
)


# --- find_poi_needs_spatial_scope gate ------------------------------------


def _parse_args(call: dict[str, Any]) -> dict[str, Any]:
    raw = call.get("arguments")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _find_poi_needs_spatial_scope_check(
    proposed: list[dict[str, Any]],
) -> GateViolation | None:
    """Fire when `geo.find_poi` is proposed with no lat/lng or bbox AND no
    sibling `geo.address_lookup` call exists in the batch.

    Background: post-v0.6.0 collapse, gpt-oss-120b occasionally calls
    `geo.find_poi(category=...)` directly on Cantonese POI queries, skipping
    the `geo.address_lookup` precondition the chain_rules engine relies on.
    The args validator then rejects (spatial scope is required) and the
    chain rule can't fire (no precondition result). This gate catches that
    shape pre-execution and re-prompts with a clear "address_lookup first"
    hint, restoring the v0.5.x chain reliability without baking the rule
    into a tool description that the LLM might ignore again.
    """
    poi_call = next(
        (c for c in proposed if c.get("name") == "geo.find_poi"),
        None,
    )
    if poi_call is None:
        return None
    args = _parse_args(poi_call)
    has_point = args.get("lat") is not None and args.get("lng") is not None
    has_full_bbox = all(
        args.get(k) is not None for k in ("min_lat", "min_lng", "max_lat", "max_lng")
    )
    if has_point or has_full_bbox:
        return None  # already has spatial scope — handler will run fine.
    # Spatial scope missing. A sibling `geo.address_lookup` call would
    # produce coords AFTER both calls run, but the dispatcher validates
    # per-call before that, so the find_poi call would still fail.
    #
    # We have a `corrective_prompt` for the re-prompt path. Live smoke
    # also showed gpt-oss-120b can keep producing the SAME bare-find_poi
    # shape on the retry (especially for Cantonese POI queries). The
    # orchestrator detects that case by `violation.kind == "missing_spatial_scope"`
    # and substitutes the proposal with a deterministic `geo.address_lookup`
    # call — see `smcity/orchestrator.py` `_rectify_missing_spatial_scope`.
    # The chain_rules POI engine then auto-dispatches the matching find_poi
    # once coords resolve.
    return GateViolation(
        name="find_poi_needs_spatial_scope",
        kind="missing_spatial_scope",
        corrective_prompt=(
            "You proposed `geo.find_poi` without `lat`/`lng` or a full bbox. "
            "That call will fail validation. Call `geo.address_lookup` FIRST "
            "to resolve the landmark in the user's question to coordinates, "
            "THEN call `geo.find_poi` with those `lat`/`lng` values and the "
            "matching `category` slug. Both calls go in the SAME tool batch — "
            "the orchestrator runs them in parallel and chains the results."
        ),
    )


FIND_POI_NEEDS_SPATIAL_SCOPE_GATE = ToolCallGate(
    name="find_poi_needs_spatial_scope",
    check=_find_poi_needs_spatial_scope_check,
)


# --- registered gates -----------------------------------------------------
#
# Add new gates here. Order matters — earlier gates fire first when
# multiple would apply (the engine returns on first match).

DEFAULT_GATES: list[ToolCallGate] = [
    ASK_USER_ONLY_GATE,
    FIND_POI_NEEDS_SPATIAL_SCOPE_GATE,
]


__all__ = [
    "ASK_USER_ONLY_GATE",
    "DEFAULT_GATES",
    "FIND_POI_NEEDS_SPATIAL_SCOPE_GATE",
    "GateViolation",
    "ToolCallGate",
    "apply_gates",
]
