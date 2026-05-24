"""Tests for the declarative chain-completion engine.

Every chain rule we add (POI today, transport/facility tomorrow) will plug
into `apply_chain_rules`. The engine semantics — precondition matching,
successor exclusion, predicate gating, resolver dispatch — are tested here
so adding a new rule needs only a rule-specific test.
"""

from __future__ import annotations

from smcity.chain_rules import (
    DEFAULT_RULES,
    POI_CHAIN_RULE,
    AutoDispatch,
    ChainRule,
    LLMHint,
    apply_chain_rules,
)
from smcity.tools.registry import ToolResult


def _ok(name: str, result: dict | None = None) -> ToolResult:
    return ToolResult(
        name=name,
        args={},
        status="ok",
        latency_ms=10,
        result=result or {},
    )


def _err(name: str, error: str = "fail") -> ToolResult:
    return ToolResult(name=name, args={}, status="error", latency_ms=10, error=error)


def _address_lookup_result(lat: float = 22.30, lng: float = 114.17) -> dict:
    return {"candidates": [{"lat": lat, "lng": lng, "name_en": "Tsim Sha Tsui", "name": "尖沙咀"}]}


# --- engine semantics -----------------------------------------------------


def test_engine_returns_none_when_no_rule_matches() -> None:
    """Empty tool list → nothing to complete."""
    assert apply_chain_rules("where is the nearest bench?", []) is None


def test_engine_returns_none_when_precondition_not_fired() -> None:
    """Other tool fired, but not the precondition → rule shouldn't trigger."""
    results = [_ok("context.get_current_weather", {"temperature_c": 25})]
    assert apply_chain_rules("nearest bench?", results) is None


def test_engine_returns_none_when_precondition_errored() -> None:
    """Precondition tool errored — chain wasn't really established."""
    results = [_err("geo.address_lookup")]
    assert apply_chain_rules("nearest bench near TST?", results) is None


def test_engine_returns_none_when_successor_already_fired() -> None:
    """If the chain already completed, we mustn't re-fire."""
    results = [
        _ok("geo.address_lookup", _address_lookup_result()),
        _ok("geo.find_bench", {"pois": []}),
    ]
    assert apply_chain_rules("nearest bench near TST?", results) is None


def test_engine_returns_none_when_question_not_poi_shaped() -> None:
    """Address lookup fired but the question isn't POI-shaped."""
    results = [_ok("geo.address_lookup", _address_lookup_result())]
    assert apply_chain_rules("how's the weather in TST?", results) is None


def test_engine_skips_rule_when_resolver_returns_none() -> None:
    """If precondition result has no candidates, resolver returns None and
    the engine should fall through to the next rule (here: no other rule
    matches, so we get None overall)."""
    results = [_ok("geo.address_lookup", {"candidates": []})]
    assert apply_chain_rules("nearest bench?", results) is None


def test_engine_evaluates_rules_in_order() -> None:
    """Earlier rules win when both could match. Demonstrated with a stub rule
    inserted ahead of the real POI rule."""
    stub_fired: list[bool] = []

    def _stub_resolver(text: str, lookup: ToolResult) -> AutoDispatch:
        stub_fired.append(True)
        return AutoDispatch(tool="stub.tool", args={})

    stub_rule = ChainRule(
        name="stub",
        precondition_tool="geo.address_lookup",
        required_successor_names=frozenset({"stub.tool"}),
        question_predicate=lambda _t: True,
        resolver=_stub_resolver,
    )
    results = [_ok("geo.address_lookup", _address_lookup_result())]
    match = apply_chain_rules("nearest bench?", results, rules=[stub_rule, POI_CHAIN_RULE])
    assert match is not None
    rule, continuation = match
    assert rule.name == "stub"
    assert isinstance(continuation, AutoDispatch)
    assert continuation.tool == "stub.tool"
    assert stub_fired == [True]


# --- POI rule: AutoDispatch on category inference ------------------------


def test_poi_rule_auto_dispatches_when_category_inferrable() -> None:
    results = [_ok("geo.address_lookup", _address_lookup_result())]
    match = apply_chain_rules("nearest bench near TST?", results)
    assert match is not None
    rule, continuation = match
    assert rule is POI_CHAIN_RULE
    assert isinstance(continuation, AutoDispatch)
    assert continuation.tool == "geo.find_bench"
    assert continuation.args["lat"] == 22.30
    assert continuation.args["lng"] == 114.17


def test_poi_rule_dispatches_dentist_in_english() -> None:
    results = [_ok("geo.address_lookup", _address_lookup_result())]
    match = apply_chain_rules("find a dentist near TST", results)
    assert match is not None
    _, continuation = match
    assert isinstance(continuation, AutoDispatch)
    assert continuation.tool == "geo.find_dentist"


def test_poi_rule_dispatches_dentist_in_traditional_chinese() -> None:
    results = [_ok("geo.address_lookup", _address_lookup_result())]
    match = apply_chain_rules("尖沙咀附近邊度有牙醫?", results)
    assert match is not None
    _, continuation = match
    assert isinstance(continuation, AutoDispatch)
    assert continuation.tool == "geo.find_dentist"


def test_poi_rule_dispatches_dentist_in_simplified_chinese() -> None:
    results = [_ok("geo.address_lookup", _address_lookup_result())]
    match = apply_chain_rules("尖沙咀附近哪里有牙医?", results)
    assert match is not None
    _, continuation = match
    assert isinstance(continuation, AutoDispatch)
    assert continuation.tool == "geo.find_dentist"


def test_poi_rule_dispatches_convenience_store_for_brand_names() -> None:
    """Brand names (7-eleven, Circle K) should route to convenience_store."""
    results = [_ok("geo.address_lookup", _address_lookup_result())]
    for query in ("nearest 7-eleven near TST", "where is Circle K near TST"):
        match = apply_chain_rules(query, results)
        assert match is not None, query
        _, continuation = match
        assert isinstance(continuation, AutoDispatch)
        assert continuation.tool == "geo.find_convenience_store", query


def test_poi_rule_dispatches_toilet_across_synonyms() -> None:
    results = [_ok("geo.address_lookup", _address_lookup_result())]
    for query in ("nearest toilet", "where's the public restroom", "公廁喺邊"):
        match = apply_chain_rules(query, results)
        assert match is not None, query
        _, continuation = match
        assert isinstance(continuation, AutoDispatch)
        assert continuation.tool == "geo.find_public_toilet", query


# --- POI rule: LLMHint fallback ------------------------------------------


def test_poi_rule_falls_back_to_llm_hint_when_category_ambiguous() -> None:
    """POI-shaped question with no known category keyword → LLMHint, not
    AutoDispatch. The LLM picks which find_* tool to fire."""
    results = [_ok("geo.address_lookup", _address_lookup_result())]
    # "thing" isn't in any category pattern — generic POI shape only.
    match = apply_chain_rules("where is the nearest thing to do near TST?", results)
    assert match is not None
    _, continuation = match
    assert isinstance(continuation, LLMHint)
    assert "lat=22.3" in continuation.text
    assert "lng=114.17" in continuation.text
    assert "Tsim Sha Tsui" in continuation.text


# --- DEFAULT_RULES sanity ------------------------------------------------


def test_default_rules_contain_poi_rule() -> None:
    assert POI_CHAIN_RULE in DEFAULT_RULES
