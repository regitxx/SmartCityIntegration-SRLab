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
            "geo.address_lookup for landmark queries, a geo.find_<category> "
            "tool for nearest-X queries). The search tool's response — even "
            "an empty list — is more useful than a clarifying question. "
            "Only fall back to meta.ask_user if no search tool fits."
        ),
    )


ASK_USER_ONLY_GATE = ToolCallGate(
    name="ask_user_only_first_move",
    check=_ask_user_only_check,
)


# --- registered gates -----------------------------------------------------
#
# Add new gates here. Order matters — earlier gates fire first when
# multiple would apply (the engine returns on first match).

DEFAULT_GATES: list[ToolCallGate] = [
    ASK_USER_ONLY_GATE,
]


__all__ = [
    "ASK_USER_ONLY_GATE",
    "DEFAULT_GATES",
    "GateViolation",
    "ToolCallGate",
    "apply_gates",
]
