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
from re import Pattern
from typing import Any

from smcity.tools.osm_pois import POI_TOOL_NAME, POI_TOOL_NAMES
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
# Shape regex catches the "is this a POI/nearest-X question?" sniff test.
# Category regex catches "which OSM category did the user mean?" so we can
# auto-dispatch the matching geo.find_<slug> tool.
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

# slug → keyword pattern. Order matters: more specific brand/noun patterns
# first, generic categories last. The first match wins.
#
# Each pattern combines EN + zh-Hant + zh-Hans variants so we route the same
# whether the user typed in English, Cantonese, or simplified Chinese.
_POI_CATEGORY_PATTERNS: dict[str, Pattern[str]] = {
    # --- unambiguous brand / specialist nouns -----------------------------
    "dentist": re.compile(r"\b(dentist|dental)\b|牙醫|牙医", re.IGNORECASE),
    "veterinarian": re.compile(r"\b(vet|veterinarian|animal\s+clinic)\b|獸醫|兽医", re.IGNORECASE),
    "optician": re.compile(r"\b(optician|eyewear|glasses\s+shop)\b|眼鏡舖|眼镜店", re.IGNORECASE),
    "bookmaker": re.compile(
        r"\b(jockey\s+club|off-course|betting\s+shop)\b|馬會|马会", re.IGNORECASE
    ),
    "mtr_station_entrance": re.compile(
        r"\bmtr\s+(entrance|exit|station)\b|港鐵.*(出入口|出口|入口)|地鐵.*(出口|入口)",
        re.IGNORECASE,
    ),
    "drinking_water": re.compile(
        r"\b(drinking\s+water|water\s+fountain|water\s+dispenser)\b|飲水機|饮水机",
        re.IGNORECASE,
    ),
    "recycling_location": re.compile(
        r"\b(recycling|recycle\s+bin|recycling\s+point)\b|回收站|回收箱|回收點|回收点",
        re.IGNORECASE,
    ),
    "marketplace": re.compile(
        r"\b(wet\s+market|marketplace|public\s+market)\b|街市|菜市場|菜市场", re.IGNORECASE
    ),
    "public_toilet": re.compile(
        r"\b(public\s+toilet|public\s+restroom|public\s+washroom|toilet|restroom|washroom|loo)\b"
        r"|公廁|公厕|廁所|厕所|洗手間|洗手间",
        re.IGNORECASE,
    ),
    "place_of_worship": re.compile(
        r"\b(temple|church|mosque|shrine|place\s+of\s+worship)\b|廟宇|寺廟|寺庙|教堂|清真寺",
        re.IGNORECASE,
    ),
    "government_office": re.compile(
        r"\b(government\s+office|district\s+office|home\s+affairs)\b|政府辦事處|政府办事处|民政事務處",
        re.IGNORECASE,
    ),
    # --- transit + infrastructure -----------------------------------------
    "public_elevator": re.compile(
        r"\b(public\s+lift|public\s+elevator|street\s+elevator|footbridge\s+lift)\b"
        r"|升降機|升降机|電梯|电梯",
        re.IGNORECASE,
    ),
    # NB: `\b(benches?)\b` has a Python re quirk where the optional `s`
    # inside the group fails to match the bare "bench" case. Use explicit
    # alternation instead.
    "bench": re.compile(
        r"\bbench(?:es)?\b|\bpublic\s+seat\b|長凳|长凳|休憩座椅|公眾座椅",
        re.IGNORECASE,
    ),
    "shelter": re.compile(
        r"\b(rain\s+shelter|bus\s+shelter|public\s+shelter|awning)\b|涼亭|凉亭|遮蔭處|遮阴处",
        re.IGNORECASE,
    ),
    "handrail": re.compile(r"\b(handrail|railing|grab\s+rail)\b|扶手", re.IGNORECASE),
    # --- shops: specific brand-shaped -------------------------------------
    "convenience_store": re.compile(
        r"\b(convenience\s+store|7-?eleven|7-?11|circle\s*k|vango)\b|便利店|7-11|7仔",
        re.IGNORECASE,
    ),
    "supermarket": re.compile(
        r"\b(supermarket|wellcome|park\s*n\s*shop|aeon|fusion)\b|超市|超級市場|超级市场",
        re.IGNORECASE,
    ),
    "department_store": re.compile(
        r"\b(department\s+store|sogo|yata|lane\s+crawford)\b|百貨公司|百货公司|百貨",
        re.IGNORECASE,
    ),
    "variety_store": re.compile(
        r"\b(variety\s+store|dollar\s+store|japan\s+home|don\s+don)\b|日本城|多多",
        re.IGNORECASE,
    ),
    "kiosk": re.compile(r"\b(kiosk|news\s*stand|newsagent)\b|報攤|报摊", re.IGNORECASE),
    # --- shops: generic category nouns ------------------------------------
    "hardware_store": re.compile(r"\bhardware\s+(store|shop)\b|五金舖|五金店", re.IGNORECASE),
    "hairdresser": re.compile(
        r"\b(hairdresser|barber|hair\s+salon|barbershop)\b|髮型屋|理髮店|理发店|髮廊",
        re.IGNORECASE,
    ),
    "clothes_shop": re.compile(
        r"\b(clothes\s+(shop|store)|clothing\s+(shop|store)|apparel)\b|服裝店|服装店|衫舖",
        re.IGNORECASE,
    ),
    "electronics_shop": re.compile(
        r"\b(electronics\s+(shop|store)|gadget\s+shop)\b|電器店|电器店|電子產品店",
        re.IGNORECASE,
    ),
    "houseware_shop": re.compile(
        r"\b(houseware|household\s+goods|home\s+goods)\b|家品店|家居用品店", re.IGNORECASE
    ),
    "beauty_shop": re.compile(
        r"\b(beauty\s+(shop|store)|cosmetics\s+(shop|store)|sasa|bonjour)\b|美妝店|美妆店|化妝品店",
        re.IGNORECASE,
    ),
    "shoe_shop": re.compile(r"\b(shoe\s+(shop|store))\b|鞋舖|鞋店", re.IGNORECASE),
    "greengrocer": re.compile(
        r"\b(greengrocer|fruit\s+(shop|store)|vegetable\s+(shop|store))\b"
        r"|生果舖|生果店|菜舖|蔬果店",
        re.IGNORECASE,
    ),
    "bookstore": re.compile(
        r"\b(bookstore|book\s*shop|bookshop)\b|書店|书店|書局|书局", re.IGNORECASE
    ),
    "laundry": re.compile(
        r"\b(laundry|laundromat|laundrette|dry\s+clean(er|ing))\b|洗衣店|乾洗店|干洗店",
        re.IGNORECASE,
    ),
}


def _infer_poi_category(user_text: str) -> str | None:
    """Pick the POI slug whose keyword pattern matches the user's text.

    Returns None if no pattern matches (the resolver falls back to LLMHint).
    """
    if not user_text:
        return None
    for slug, pattern in _POI_CATEGORY_PATTERNS.items():
        if pattern.search(user_text):
            return slug
    return None


def _poi_question_predicate(text: str) -> bool:
    """True for any text that smells POI-shaped — either by question shape
    ('nearest', '附近', 'where is') or by mentioning a known POI category."""
    if not text:
        return False
    if _POI_SHAPE_RE.search(text):
        return True
    return _infer_poi_category(text) is not None


def _poi_resolver(user_text: str, lookup: ToolResult) -> ChainContinuation | None:
    """Build the chain continuation for an incomplete POI chain.

    `lookup` is the successful `geo.address_lookup` result. We pull the first
    candidate's lat/lng and decide:
    - If the user's text names a specific category → AutoDispatch the
      matching geo.find_<slug> tool with those coords (deterministic).
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

    category = _infer_poi_category(user_text)
    if category is not None:
        return AutoDispatch(
            tool=POI_TOOL_NAME[category],
            args={
                "lat": float(lat),
                "lng": float(lng),
                "radius_m": 800,
                "max_results": 20,
            },
        )
    return LLMHint(
        text=(
            f"You called geo.address_lookup and got lat={lat}, lng={lng} "
            f"({place}), but you did NOT call a geo.find_* POI tool. The user "
            "asked a POI / 'where is the nearest …' question. Call the matching "
            "geo.find_<category> tool NOW with those coordinates. Do not "
            "synthesise a reply yet."
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
