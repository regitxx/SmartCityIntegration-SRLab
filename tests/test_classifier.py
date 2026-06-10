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


def test_located_weather_question_bails_to_full_llm_path() -> None:
    # Structural rule, not a city list: ANY named place defers — the LLM owns
    # the judgement of whether it's in scope (HKO data is territory-wide).
    assert classify("what's the weather in Tokyo?") is None
    assert classify("東京天氣點？") is None
    assert classify("沙田天氣點？") is None
    assert classify("weather in London please") is None


def test_hk_itself_is_not_a_located_weather_question() -> None:
    hit = classify("air quality in HK?")
    assert hit is not None and hit.intent == "aqi"
    hit2 = classify("香港天氣點？")
    assert hit2 is not None and hit2.intent == "weather"


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


@pytest.mark.parametrize(
    ("text", "reply_fragment"),
    [
        ("唔該晒", "唔使客氣"),
        ("多謝!", "唔使客氣"),
        ("thanks", "welcome"),
        ("ありがとう", "どういたしまして"),
        ("감사합니다", "천만에요"),
    ],
)
def test_chitchat_thanks_replies_in_the_users_language(text: str, reply_fragment: str) -> None:
    # The chitchat path has NO synthesis hop, so the wrong_language invariant
    # can't catch it — the canned table itself must be language-correct.
    hit = classify(text)
    assert hit is not None and hit.intent == "chitchat", text
    assert hit.reply_if_chitchat and reply_fragment in hit.reply_if_chitchat


def test_thanks_prefix_does_not_swallow_a_real_request() -> None:
    # "唔該" here is "excuse me", not "thanks" — must NOT return the canned reply.
    hit = classify("唔該，附近有冇廁所？")
    assert hit is None or hit.intent != "chitchat"


def test_transport_query_is_not_fast_path() -> None:
    assert classify("how do I get from Sheung Wan to Sha Tin?") is None
    assert classify("我喺上環，下班車幾時到？") is None
    assert classify("basketball court near Sheung Wan?") is None


# --- POI fast path ----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "category", "location"),
    [
        ("any supermarkets near Mong Kok?", "supermarket", "Mong Kok"),
        ("美孚附近有冇超市？", "supermarket", "美孚"),
        ("喺旺角搵書店", "bookstore", "旺角"),
        ("尖沙咀有冇公廁？", "public_toilet", "尖沙咀"),
        # Simplified input: the phrase is taken from the raw half (中环) —
        # corroboration in the orchestrator normalises scripts before matching.
        ("中环附近最近的眼镜店在哪里？", "optician", "中环"),
        ("where can I find a laundry in Wan Chai", "laundry", "Wan Chai"),
    ],
)
def test_poi_with_location_phrase(text: str, category: str, location: str) -> None:
    hit = classify(text)
    assert hit is not None, text
    assert hit.intent == "poi"
    assert hit.poi_category == category
    assert hit.location == location
    assert hit.location_is_self is False


@pytest.mark.parametrize(
    "text",
    ["附近有冇廁所？", "supermarket near me please", "最近嘅書店喺邊度？"],
)
def test_poi_near_me_uses_session_location(text: str) -> None:
    hit = classify(text)
    assert hit is not None, text
    assert hit.intent == "poi"
    assert hit.location_is_self is True
    assert hit.location is None


@pytest.mark.parametrize(
    "text",
    [
        "supermarket",  # no location signal at all
        "香港有冇投注站？",  # territory-wide — too broad for a radius search
        "supermarket and wet market near Mong Kok",  # two categories — ambiguous
        "how do I get to the supermarket near Mei Foo?",  # directions, not a POI list
        "聽日邊度有街市開？",  # time-qualified — needs opening-hours judgement
        "美孚附近天氣點，有冇超市？",  # cross-domain mix
    ],
)
def test_ambiguous_poi_defers_to_full_llm_path(text: str) -> None:
    hit = classify(text)
    assert hit is None or hit.intent != "poi", text
