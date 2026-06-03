# CJK glyphs in keywords + prose are intentional.
"""Declarative chain-completion rules for the orchestrator.

A `ChainRule` says: "when tool X fired successfully on a turn, but none of
the expected successor tools Y did, and the user's question has the shape
the rule cares about — produce a continuation telling the orchestrator how
to finish the chain."

There are two continuation shapes:

- `AutoDispatch(tool, args)` — deterministic. The orchestrator fires the
  successor tool itself, skipping a second LLM hop. Use this when the
  successor + its args can be inferred unambiguously from the precondition
  result and the user's text.

- `LLMHint(text)` — fallback. The orchestrator appends `text` as a system
  message and re-prompts the LLM to choose the successor itself. Use this
  when inference is ambiguous (multiple plausible successors, or args that
  need LLM judgement).

The POI rule (the original "Fix 3") is the first rule registered. New rules
(facility lookup → find_courts, transport stop lookup → find_stops, …) plug
into the same engine without orchestrator surgery.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from smcity.tools.osm_pois import POI_TOOL, POI_TOOL_NAMES
from smcity.tools.poi_categories import categorize
from smcity.tools.registry import ToolResult

# --- continuation types ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class AutoDispatch:
    """Fire `tool` with `args` directly from the orchestrator. No LLM re-roll."""

    tool: str
    args: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LLMHint:
    """Append `text` as a system message and let the LLM pick the successor."""

    text: str


ChainContinuation = AutoDispatch | LLMHint


# --- rule shape -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChainRule:
    """Declarative condition + resolver for an incomplete tool chain.

    A rule fires when ALL of:
    - `precondition_tool` ran with status="ok" this turn.
    - None of `required_successor_names` ran (any status).
    - `question_predicate(user_text)` returns True.

    Then `resolver(user_text, precondition_result)` is called and its result
    is returned. The resolver may return None if it cannot produce a valid
    continuation (e.g. the precondition result is empty); in that case the
    chain is left alone.
    """

    name: str  # for logging / telemetry
    precondition_tool: str
    required_successor_names: frozenset[str]
    question_predicate: Callable[[str], bool]
    resolver: Callable[[str, ToolResult], ChainContinuation | None]


# --- engine ---------------------------------------------------------------


def apply_chain_rules(
    user_text: str,
    tool_results: list[ToolResult],
    rules: Iterable[ChainRule] | None = None,
) -> tuple[ChainRule, ChainContinuation] | None:
    """Return the first (rule, continuation) pair that wants to fire, or None.

    Rules are evaluated in order. The first one whose preconditions match AND
    whose resolver returns a non-None continuation wins. Earlier rules
    therefore take precedence — order `DEFAULT_RULES` accordingly.
    """
    if rules is None:
        rules = DEFAULT_RULES

    ok_by_name: dict[str, ToolResult] = {r.name: r for r in tool_results if r.status == "ok"}
    fired_any = {r.name for r in tool_results}  # includes errored/timeout calls

    for rule in rules:
        precondition_result = ok_by_name.get(rule.precondition_tool)
        if precondition_result is None:
            continue
        if fired_any & rule.required_successor_names:
            continue  # a successor already ran (we don't re-evaluate based on status)
        if not rule.question_predicate(user_text):
            continue
        continuation = rule.resolver(user_text, precondition_result)
        if continuation is not None:
            return (rule, continuation)
    return None


# --- POI rule -------------------------------------------------------------
#
# Two independent sniffs decide whether this turn is an unfinished POI lookup
# we must complete (address_lookup ran, find_poi didn't):
#   - SHAPE  — the question reads like a "where / nearest / find" request, or
#   - CATEGORY — `categorize()` recognises a POI category in the user's text.
# Either is sufficient. The category lexicon and its Simplified↔Traditional +
# plural-tolerance matching mechanisms live in `smcity/tools/poi_categories.py`
# — the single source of truth shared with the LLM-facing `find_poi` schema,
# so the router's notion of "what words mean beauty_shop" can never drift from
# the model's.
#
# The shape regex is intentionally generous — false positives cost one LLM
# round-trip, false negatives skip the chain enforcement entirely.

_POI_SHAPE_RE = re.compile(
    r"""
    nearest | closest | near\s+me | nearby | around | find\s+a | find\s+the
    | where\s+is | where\s+can\s+i\s+find | how\s+do\s+i\s+find | show\s+me
    | 最近 | 附近 | 邊度有 | 邊度可以 | 揾.*喺邊 | 搵.*喺邊
    | 點樣搵 | 邊間 | 有冇.*喺
    | 哪里 | 哪裏 | 最近的 | 在哪
    """,
    re.IGNORECASE | re.VERBOSE,
)

def _poi_question_predicate(text: str) -> bool:
    """True for any text that smells POI-shaped — either by question shape
    ('nearest', '附近', 'where is') or by naming a known POI category.

    Category recognition is delegated to `poi_categories.categorize`, the
    shared registry router — so the chain rule fires for exactly the phrasings
    the deterministic dispatch can act on, with no second keyword table here.
    """
    if not text:
        return False
    if _POI_SHAPE_RE.search(text):
        return True
    return categorize(text) is not None


def _poi_resolver(user_text: str, lookup: ToolResult) -> ChainContinuation | None:
    """Build the chain continuation for an incomplete POI chain.

    `lookup` is the successful `geo.address_lookup` result. We pull the first
    candidate's lat/lng and decide:
    - If the user's text names a specific category → AutoDispatch
      `geo.find_poi(category=<slug>, lat=..., lng=...)` (deterministic).
    - Otherwise → LLMHint with the coords pre-quoted, so the LLM picks.
    """
    candidates = (lookup.result or {}).get("candidates") or []
    if not candidates:
        return None
    first = candidates[0]
    lat = first.get("lat")
    lng = first.get("lng") or first.get("lon")
    if lat is None or lng is None:
        return None
    place = first.get("name_en") or first.get("name") or "the resolved point"

    category = categorize(user_text)
    if category is not None:
        return AutoDispatch(
            tool=POI_TOOL,
            args={
                "category": category,
                "lat": float(lat),
                "lng": float(lng),
                "radius_m": 800,
                "max_results": 20,
            },
        )
    return LLMHint(
        text=(
            f"You called geo.address_lookup and got lat={lat}, lng={lng} "
            f"({place}), but you did NOT call `geo.find_poi`. The user asked "
            "a POI / 'where is the nearest …' question. Call `geo.find_poi` "
            "NOW with those coordinates and the matching `category` slug. "
            "Do not synthesise a reply yet."
        )
    )


POI_CHAIN_RULE = ChainRule(
    name="poi_address_to_find",
    precondition_tool="geo.address_lookup",
    required_successor_names=POI_TOOL_NAMES,
    question_predicate=_poi_question_predicate,
    resolver=_poi_resolver,
)


# --- registered rules -----------------------------------------------------
#
# Add new rules here. Order matters — earlier rules take precedence when
# multiple match the same turn.

DEFAULT_RULES: list[ChainRule] = [
    POI_CHAIN_RULE,
]


__all__ = [
    "DEFAULT_RULES",
    "POI_CHAIN_RULE",
    "AutoDispatch",
    "ChainContinuation",
    "ChainRule",
    "LLMHint",
    "apply_chain_rules",
]
