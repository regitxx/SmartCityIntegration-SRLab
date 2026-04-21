"""Pre-classifier unit tests."""
# ruff: noqa: RUF001

from __future__ import annotations

import pytest

from smcity.classifier import classify


@pytest.mark.parametrize(
    ("text", "intent", "tools"),
    [
        # weather — fast path
        ("what's the weather?", "weather", ["context.get_current_weather"]),
        ("How's the weather right now", "weather", ["context.get_current_weather"]),
        ("而家天氣點呀？", "weather", ["context.get_current_weather"]),
        ("现在天气怎么样？", "weather", ["context.get_current_weather"]),
        ("今の天気はどうですか?", "weather", ["context.get_current_weather"]),
        ("날씨 어때요", "weather", ["context.get_current_weather"]),
        # AQI
        ("what's the aqi?", "aqi", ["context.get_aqhi"]),
        ("air quality in HK?", "aqi", ["context.get_aqhi"]),
        ("空氣質素點？", "aqi", ["context.get_aqhi"]),
        # warnings
        ("any typhoon warning?", "warnings", ["context.get_active_warnings"]),
        ("有冇颱風警告？", "warnings", ["context.get_active_warnings"]),
    ],
)
def test_single_intent_fast_paths(text: str, intent: str, tools: list[str]) -> None:
    hit = classify(text)
    assert hit is not None, text
    assert hit.intent == intent
    assert hit.tools == tools


def test_combined_outdoor_activity_fires_weather_plus_air_plus_warnings() -> None:
    hit = classify("is it ok to go outside now?")
    assert hit is not None
    assert hit.intent == "weather_and_air"
    assert "context.get_current_weather" in hit.tools
    assert "context.get_aqhi" in hit.tools
    assert "context.get_active_warnings" in hit.tools


def test_future_weather_question_bails_to_full_llm_path() -> None:
    assert classify("will it rain tomorrow?") is None
    assert classify("聽日會唔會落雨？") is None


def test_non_hk_location_bails_to_full_llm_path() -> None:
    assert classify("what's the weather in Tokyo?") is None
    assert classify("東京天氣點？") is None


def test_chitchat_returns_canned_reply() -> None:
    hit = classify("hi")
    assert hit is not None
    assert hit.intent == "chitchat"
    assert hit.tools == []
    assert hit.reply_if_chitchat and "HK" in hit.reply_if_chitchat

    hit_yue = classify("哈囉")
    assert hit_yue is not None
    assert hit_yue.intent == "chitchat"
    assert hit_yue.reply_if_chitchat and "香港" in hit_yue.reply_if_chitchat


def test_transport_query_is_not_fast_path() -> None:
    assert classify("how do I get from Sheung Wan to Sha Tin?") is None
    assert classify("我喺上環，下班車幾時到？") is None
    assert classify("basketball court near Sheung Wan?") is None
