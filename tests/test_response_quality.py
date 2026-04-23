# ruff: noqa: RUF001, RUF002
"""Response-quality regression pins.

These tests anchor the prompt-and-polish behaviour that shaped past live
bugs. They don't exercise the real LLM — they assert that the *inputs*
the LLM receives, and the *post-polish* the output goes through, contain
the specific guard-strings and substitutions the system prompt relies
on. A refactor that silently drops the "Cantonese is priority" line or
loosens the "no tool names in reply" rule will fail here before it
reaches a user.

Live bugs these tests guard against:
- harmony token leak (v0.1.1)
- bare `tool_name json{...}` leak (v0.1.3)
- walking-only reply collapse (pre-v0.2)
- MTR-jump-without-asking-mode (v0.1.1)
- Mandarin-in-Cantonese register drift
- LLM-invented `src:` footer (v0.1.3)
"""

from __future__ import annotations

from smcity.cantonese_polish import polish
from smcity.langrouter.detect import LangDetection
from smcity.orchestrator import _maybe_polish, _rewrite_source_footer
from smcity.prompts import (
    SYSTEM_PROMPT,
    cantonese_style_block,
    fast_path_synthesis_hint,
    language_stick_reminder,
    locale_hint,
)
from smcity.schemas import Citation


def _det(lang: str, script: str = "Hant", tts: str = "yue-HK") -> LangDetection:
    return LangDetection(
        primary_lang=lang,
        script=script,  # type: ignore[arg-type]
        confidence=1.0,
        method="forced",  # test fixture — one of the valid literal values
        tts_locale=tts,
    )


# --- SYSTEM_PROMPT safety + routing pins ---------------------------------


def test_system_prompt_declares_cantonese_priority() -> None:
    lower = SYSTEM_PROMPT.lower()
    assert "cantonese is the priority language" in lower
    # The specific colloquial particles must be named so the LLM sees them.
    for particle in ("嘅", "喺", "咗", "冇", "佢", "唔", "係", "嗰"):
        assert particle in SYSTEM_PROMPT


def test_system_prompt_forbids_hallucinated_facts() -> None:
    # Pin the "every factual claim comes from a tool call" rule.
    assert "Every factual claim" in SYSTEM_PROMPT
    assert "tool call" in SYSTEM_PROMPT
    # Specifically name what NOT to invent (wording should mention at least 3).
    invent_hits = sum(
        kw in SYSTEM_PROMPT
        for kw in ("MTR stations", "bus routes", "weather numbers", "AQHI", "addresses")
    )
    assert invent_hits >= 3


def test_system_prompt_routes_ambiguous_travel_to_plan_journey_not_ask_user() -> None:
    """Past bug: agent asked 'MTR or bus?' for every travel query. Prompt now
    tells the LLM to call plan_journey first and compare options."""
    assert "transport.plan_journey" in SYSTEM_PROMPT
    # The guidance must explicitly say DON'T ask first for the mode.
    assert "Do NOT ask them to pick a mode first" in SYSTEM_PROMPT


def test_system_prompt_has_per_mode_routing_table() -> None:
    """Each explicit-mode keyword must route to a specific tool."""
    # MTR / 地鐵 / 港鐵 → plan_simple_route
    assert "plan_simple_route" in SYSTEM_PROMPT
    assert "地鐵" in SYSTEM_PROMPT and "港鐵" in SYSTEM_PROMPT
    # walking / 步行 / 行路 → plan_walking_route
    assert "plan_walking_route" in SYSTEM_PROMPT
    assert "步行" in SYSTEM_PROMPT and "行路" in SYSTEM_PROMPT
    # taxi / 的士 → plan_taxi_estimate
    assert "plan_taxi_estimate" in SYSTEM_PROMPT
    assert "的士" in SYSTEM_PROMPT


def test_system_prompt_forbids_harmony_tokens_in_reply() -> None:
    """Past live bug: harmony tokens leaked into the reply text. Prompt must
    explicitly tell the LLM to emit tool calls via the structured channel."""
    assert "harmony tokens" in SYSTEM_PROMPT
    for token in ("<|start|>", "<|channel|>", "<|message|>", "<|end|>"):
        assert token in SYSTEM_PROMPT
    assert "tool_calls field" in SYSTEM_PROMPT


def test_system_prompt_forbids_self_written_src_footer() -> None:
    """The service deterministically rewrites src:. Prompt must say so."""
    assert "src:" in SYSTEM_PROMPT
    assert "service" in SYSTEM_PROMPT and "NOT by you" in SYSTEM_PROMPT


# --- Cantonese style block pins -----------------------------------------


def test_cantonese_style_block_names_all_key_particles() -> None:
    block = cantonese_style_block()
    for particle in ("嘅", "喺", "咗", "冇", "佢", "唔", "係", "嗰", "啲", "咁", "點", "而家"):
        assert particle in block
    # And the Mandarin counterparts it must replace
    for mandarin in ("的", "在", "了", "沒", "他", "不", "是", "那", "些", "這樣", "怎", "現在"):
        assert mandarin in block


def test_cantonese_style_block_includes_examples() -> None:
    block = cantonese_style_block()
    assert block.count("FORMAL:") >= 6  # 6 exemplar pairs
    assert block.count("CANTO:") >= 6
    # One specific exemplar we rely on for register transfer.
    assert "而家香港" in block  # from the weather example


# --- language_stick_reminder + fast_path_synthesis_hint ------------------


def test_language_stick_reminder_forbids_bilingual_field_pullover() -> None:
    """After tool calls, the reminder must explicitly prevent the LLM from
    switching languages just because tool output was bilingual."""
    yue = language_stick_reminder(_det("yue"))
    assert "bilingual fields" in yue
    assert "the tool output" in yue
    # Cantonese-specific wording must appear for yue.
    assert "嘅/喺/咗/冇" in yue
    # And it must tell the model not to write tool names in prose.
    assert "tool names" in yue


def test_fast_path_synthesis_hint_carries_language_and_forbids_tool_names() -> None:
    hint = fast_path_synthesis_hint("weather", "- context.get_current_weather: …", _det("yue"))
    assert "'yue'" in hint  # primary_lang present
    assert "yue-HK" in hint  # tts_locale present
    assert "tool names" in hint
    assert "JSON" in hint


def test_locale_hint_marks_forced_vs_detected() -> None:
    forced = locale_hint(_det("yue"), forced=True)
    detected = locale_hint(_det("yue"), forced=False)
    assert "forced" in forced
    assert "detected" in detected
    # Both must name the target language.
    for hint in (forced, detected):
        assert "primary_lang" in hint
        assert "tts_locale" in hint


# --- polish only-on-Cantonese pin ----------------------------------------


def test_maybe_polish_only_fires_for_yue() -> None:
    """Applying Cantonese polish to an English / Mandarin reply would corrupt it."""
    mandarin = "現在香港是 27 度。"
    # Cantonese → gets polished
    out_yue = _maybe_polish(mandarin, _det("yue"))
    assert "而家" in out_yue  # 現在 → 而家
    # Mandarin (zho) → NOT polished, stays formal
    out_zho = _maybe_polish(mandarin, _det("zho", script="Hant", tts="zh-HK"))
    assert out_zho == mandarin
    # English → NOT polished
    out_eng = _maybe_polish("It's 27°C now.", _det("eng", script="Latin", tts="en-US"))
    assert out_eng == "It's 27°C now."


# --- deterministic src-footer rewrite pins (past live bug) ---------------


def _cite(tool: str) -> Citation:
    return Citation(
        tool=tool,
        upstream="test",
        fetched_at="2026-04-23T00:00:00Z",  # type: ignore[arg-type]
    )


def test_rewrite_source_footer_strips_llm_invented_fake_tools() -> None:
    reply = "Next train in 3 min.\n\nsrc: mtr_next_trains / lcsd_courts / fake_tool"
    out = _rewrite_source_footer(reply, [_cite("transport.get_mtr_next_trains")])
    assert "lcsd_courts" not in out
    assert "fake_tool" not in out
    # Real citation survives (normalised to the trailing segment).
    assert "src: get_mtr_next_trains" in out


def test_rewrite_source_footer_no_citations_strips_any_src() -> None:
    """When no real tools ran, any src: line the LLM wrote must be removed."""
    reply = "Hi there.\nsrc: pretend_tool"
    out = _rewrite_source_footer(reply, [])
    assert "src" not in out.lower()
    assert "Hi there." in out


def test_rewrite_source_footer_tolerates_fullwidth_colon() -> None:
    """Past bug: LLM wrote `src：` (fullwidth colon) in Cantonese replies."""
    reply = "次日有雨。\nsrc： 假工具"
    out = _rewrite_source_footer(reply, [])
    assert "假工具" not in out
    assert "src" not in out.lower()


def test_rewrite_source_footer_dedupes_repeated_tool() -> None:
    reply = "Reply."
    cites = [_cite("transport.get_mtr_next_trains")] * 3
    out = _rewrite_source_footer(reply, cites)
    # Dedupes — only one mention in the footer.
    assert out.count("get_mtr_next_trains") == 1


# --- polish over-eager-substitution pins (guards against accuracy bugs) --


def test_polish_does_not_corrupt_english_in_mixed_reply() -> None:
    """Code-switched reply: the English fragments must not be affected."""
    mixed = "而家係 27 度，humidity is 76 percent。"
    out = polish(mixed)
    # Mandarin 的 → 嘅 is fine; English "is" / "percent" must be untouched.
    assert "humidity is 76 percent" in out


def test_polish_does_not_rewrite_taxi_or_progressive_aspect() -> None:
    """Two known danger-zones: 的士 must stay, 正在 must stay."""
    assert "的士" in polish("搭的士去機場")
    assert "正在" in polish("列車正在離開")
    # Lexical 了解 / 了結 must not get rewritten to 咗解 / 咗結.
    assert "咗解" not in polish("我了解呢件事")
    assert "咗結" not in polish("事情已經了結")


def test_polish_does_not_mangle_proper_noun_with_in_name() -> None:
    """e.g. '現代' (modern) in a brand name shouldn't explode into '而代'."""
    # 現代 is NOT in the phrase list; verify it stays intact.
    out = polish("現代汽車喺香港嘅分店")
    assert "現代" in out


# --- orchestrator defence-in-depth pins ----------------------------------


def test_system_prompt_has_the_pre_v02_walking_bug_mitigation() -> None:
    """Pre-v0.2 bug: 'what about walking?' produced an empty reply because the
    LLM tried tool-calls on a retry. Prompt now lists plan_walking_route as
    the dedicated tool + _stream_final has retry fallback."""
    import inspect

    from smcity.orchestrator import Orchestrator

    assert "plan_walking_route" in SYSTEM_PROMPT
    # The retry reminder must tell the LLM to STOP calling tools + produce prose.
    body = inspect.getsource(Orchestrator._stream_final)
    assert "STOP CALLING TOOLS" in body
    assert "Produce a short natural-language" in body
