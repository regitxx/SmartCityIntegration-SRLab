"""Tests for the post-synthesis invariant engine.

We test the mechanism (engine + record extraction + multilingual denial
regex), not orchestrator integration — that's exercised by the existing
orchestrator test suite plus live testing.

The multilingual coverage matters because the agent serves all of EN, yue,
zh-Hant, zh-Hans, ja, ko, fr, de, es, th, vi, id, tl. A denial regex that
only catches English would silently leak bad replies in every other
language. Each language gets at least one positive case here.
"""

from __future__ import annotations

from smcity.langrouter.detect import LangDetection
from smcity.synthesis_invariants import (
    _DENIAL_PATTERN,
    DATA_DENIAL_INVARIANT,
    DEFAULT_INVARIANTS,
    InvariantViolation,
    SynthesisInvariant,
    _extract_record_names,
    apply_invariants,
)
from smcity.tools.registry import ToolResult


def _ok(name: str, result: dict | None) -> ToolResult:
    return ToolResult(name=name, args={}, status="ok", latency_ms=10, result=result or {})


def _det(lang: str = "en") -> LangDetection:
    return LangDetection(
        primary_lang=lang,
        script="Latin" if lang == "en" else "Hant",
        confidence=1.0,
        method="forced",
        tts_locale=f"{lang}-US",
    )


def _poi_result(*names: str) -> dict:
    """Build a geo.find_poi result with the given POI names."""
    return {
        "category": "dentist",
        "bbox_used": [22.30, 114.16, 22.31, 114.18],
        "pois": [{"name_en": n, "lat": 22.30, "lng": 114.17} for n in names],
    }


# --- engine semantics -----------------------------------------------------


def test_engine_returns_none_on_empty_reply() -> None:
    assert apply_invariants("", [_ok("geo.find_poi", _poi_result("Dr Chan"))], _det()) is None


def test_engine_returns_none_on_whitespace_only_reply() -> None:
    assert (
        apply_invariants("   \n  ", [_ok("geo.find_poi", _poi_result("Dr Chan"))], _det())
        is None
    )


def test_engine_returns_none_when_no_tool_results() -> None:
    assert apply_invariants("I couldn't find any data.", [], _det()) is None


def test_engine_returns_none_when_all_tool_results_empty() -> None:
    """No records returned → reply CAN legitimately say 'I couldn't find'."""
    empty_result = {"category": "dentist", "pois": []}
    violation = apply_invariants(
        "I couldn't find any dentists nearby.",
        [_ok("geo.find_poi", empty_result)],
        _det(),
    )
    assert violation is None


def test_engine_returns_none_when_reply_has_no_denial_language() -> None:
    """Reply just hedges or summarises — no denial → no violation."""
    assert (
        apply_invariants(
            "Here are several dentists near you.",
            [_ok("geo.find_poi", _poi_result("Dr Chan", "Dr Wong"))],
            _det(),
        )
        is None
    )


def test_engine_returns_none_when_reply_mentions_a_record() -> None:
    """The LLM hedged but still cited Dr Chan — not a real denial."""
    assert (
        apply_invariants(
            "I couldn't find a specialist, but Dr Chan's clinic is nearby.",
            [_ok("geo.find_poi", _poi_result("Dr Chan", "Dr Wong"))],
            _det(),
        )
        is None
    )


def test_engine_fires_when_reply_denies_non_empty_data() -> None:
    """The canonical bug: tool returned 2 dentists, reply says 'couldn't find'
    without naming either."""
    violation = apply_invariants(
        "I couldn't find any dentists in your area, sorry.",
        [_ok("geo.find_poi", _poi_result("Dr Chan", "Dr Wong"))],
        _det(),
    )
    assert isinstance(violation, InvariantViolation)
    assert violation.kind == "data_denial"
    assert violation.tool_name == "geo.find_poi"
    assert violation.record_count == 2
    # Corrective prompt names the tool + a sample record
    assert "geo.find_poi" in violation.corrective_prompt
    assert "Dr Chan" in violation.corrective_prompt


def test_engine_skips_errored_tool_results() -> None:
    """Error-status results don't count as 'returned data' even if .result is set."""
    err_result = ToolResult(
        name="geo.find_poi",
        args={},
        status="error",
        latency_ms=10,
        error="upstream failure",
        result=_poi_result("Dr Chan"),  # shouldn't be looked at
    )
    assert apply_invariants("I couldn't find any.", [err_result], _det()) is None


def test_engine_runs_invariants_in_order() -> None:
    """Earlier invariants win when both would match."""
    fired: list[str] = []

    def _stub_check(reply, results, det):
        fired.append("stub")
        return InvariantViolation(name="stub", kind="stub", corrective_prompt="stub fired")

    stub = SynthesisInvariant(name="stub", check=_stub_check)
    violation = apply_invariants(
        "I couldn't find any.",
        [_ok("geo.find_poi", _poi_result("Dr Chan"))],
        _det(),
        invariants=[stub, DATA_DENIAL_INVARIANT],
    )
    assert violation is not None
    assert violation.name == "stub"
    assert fired == ["stub"]


# --- record extraction (generic) -----------------------------------------


def test_extract_record_names_from_pois_field() -> None:
    result = _poi_result("Dr Chan", "Dr Wong", "Dr Lee")
    assert _extract_record_names(result) == ["Dr Chan", "Dr Wong", "Dr Lee"]


def test_extract_record_names_falls_through_name_keys() -> None:
    """When items have name_tc instead of name_en, we still pick it up."""
    result = {"pois": [{"name_tc": "陳醫生"}, {"name_zh": "李醫生"}]}
    assert _extract_record_names(result) == ["陳醫生", "李醫生"]


def test_extract_record_names_handles_eta_records() -> None:
    """ETA records don't have `name` — they have `route` + `destination_en`.
    Destinations come first in _NAME_KEYS because they're more user-facing
    in replies ("next bus to Star Ferry") than bare route numbers."""
    result = {
        "etas": [
            {"route": "1A", "destination_en": "Star Ferry", "minutes_until": 5},
            {"route": "8", "destination_en": "Tsim Sha Tsui", "minutes_until": 8},
        ]
    }
    names = _extract_record_names(result)
    assert names == ["Star Ferry", "Tsim Sha Tsui"]


def test_extract_record_names_falls_to_route_when_no_destination() -> None:
    """If no destination is available, route number is used as the anchor."""
    result = {"etas": [{"route": "1A", "minutes_until": 5}]}
    assert _extract_record_names(result) == ["1A"]


def test_extract_record_names_returns_empty_for_non_dict() -> None:
    assert _extract_record_names(None) == []
    assert _extract_record_names("not a dict") == []  # type: ignore[arg-type]
    assert _extract_record_names([1, 2, 3]) == []  # type: ignore[arg-type]


def test_extract_record_names_returns_empty_when_no_list_fields() -> None:
    assert _extract_record_names({"status": "ok", "count": 5}) == []


def test_extract_record_names_caps_at_limit() -> None:
    result = {"pois": [{"name_en": f"Dr {i}"} for i in range(20)]}
    names = _extract_record_names(result, limit=3)
    assert len(names) == 3
    assert names == ["Dr 0", "Dr 1", "Dr 2"]


# --- multilingual denial regex ------------------------------------------
# One canonical phrase per language. Adding more phrases for a language
# means adding to the regex in synthesis_invariants.py — these tests pin
# the minimum coverage.


def test_denial_pattern_matches_english() -> None:
    for phrase in (
        "I couldn't find any dentists.",
        "I was unable to locate that.",
        "Sorry, I can't find a match.",
        "No results available.",
        "I don't have that information.",
    ):
        assert _DENIAL_PATTERN.search(phrase) is not None, phrase


def test_denial_pattern_matches_cantonese() -> None:
    for phrase in ("搵唔到呀", "對唔住,搵唔到資料", "我冇嗰啲資料"):
        assert _DENIAL_PATTERN.search(phrase) is not None, phrase


def test_denial_pattern_matches_traditional_chinese() -> None:
    for phrase in ("找不到相關資料", "沒有相關資料"):
        assert _DENIAL_PATTERN.search(phrase) is not None, phrase


def test_denial_pattern_matches_simplified_chinese() -> None:
    for phrase in ("抱歉,找不到", "我没有相关信息", "没有相关数据"):
        assert _DENIAL_PATTERN.search(phrase) is not None, phrase


def test_denial_pattern_matches_japanese() -> None:
    for phrase in ("見つかりませんでした", "情報がありません"):
        assert _DENIAL_PATTERN.search(phrase) is not None, phrase


def test_denial_pattern_matches_korean() -> None:
    for phrase in ("찾을 수 없습니다", "정보가 없습니다"):
        assert _DENIAL_PATTERN.search(phrase) is not None, phrase


def test_denial_pattern_matches_french() -> None:
    for phrase in ("Je n'ai pas trouvé de résultats.", "Aucun résultat disponible."):
        assert _DENIAL_PATTERN.search(phrase) is not None, phrase


def test_denial_pattern_matches_german() -> None:
    for phrase in ("Leider nicht gefunden.", "Keine Ergebnisse verfügbar."):
        assert _DENIAL_PATTERN.search(phrase) is not None, phrase


def test_denial_pattern_matches_spanish() -> None:
    for phrase in ("Lo siento, no encontré nada.", "Sin resultados disponibles."):
        assert _DENIAL_PATTERN.search(phrase) is not None, phrase


def test_denial_pattern_matches_thai() -> None:
    assert _DENIAL_PATTERN.search("ไม่พบข้อมูล") is not None


def test_denial_pattern_matches_vietnamese() -> None:
    assert _DENIAL_PATTERN.search("Không tìm thấy thông tin.") is not None


def test_denial_pattern_matches_indonesian() -> None:
    assert _DENIAL_PATTERN.search("Tidak ditemukan data terkait.") is not None


def test_denial_pattern_matches_tagalog() -> None:
    assert _DENIAL_PATTERN.search("Hindi mahanap ang inyong hinahanap.") is not None


def test_denial_pattern_does_not_match_neutral_text() -> None:
    """A reply that just lists records should NOT trip the regex."""
    for phrase in (
        "Here are 3 dentists near you: Dr Chan, Dr Wong, Dr Lee.",
        "MTR next trains: 中環方向 in 2 min, 上環方向 in 4 min.",
        "Walking time is about 8 minutes.",
    ):
        assert _DENIAL_PATTERN.search(phrase) is None, phrase


# --- DEFAULT_INVARIANTS sanity -------------------------------------------


def test_default_invariants_contain_data_denial() -> None:
    assert DATA_DENIAL_INVARIANT in DEFAULT_INVARIANTS
