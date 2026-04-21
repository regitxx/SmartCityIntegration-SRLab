# ruff: noqa: RUF001  # CJK fullwidth punctuation is intentional in user-facing replies.
"""Deterministic pre-classifier for trivial intents.

When a user's turn is unambiguously "what's the weather", "what's the AQI",
"any warnings", or pure chitchat ("hi", "thanks"), we can skip the first LLM
hop entirely — call the tool directly (or return a tiny canned reply) and
synthesise the final message in one LLM call instead of two. That halves
latency for the most common intents.

Rules are deliberately narrow. If a pattern is ambiguous (e.g. "will it rain
in Sha Tin tomorrow?" — that's weather *plus* a location + day slot), we
defer to the full LLM path. Classifier confidence is binary: either we have
a clear fast-path intent or we don't.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

FastPathIntent = Literal["weather", "aqi", "warnings", "weather_and_air", "chitchat"]


@dataclass(slots=True)
class FastPathHit:
    intent: FastPathIntent
    tools: list[str]  # tool names to call directly (empty for chitchat)
    reply_if_chitchat: str | None = None  # when intent == chitchat


# --- pattern banks --------------------------------------------------------
# Anchored whole-word / substring matches in 4 language families. Keep narrow.

_WEATHER_PATTERNS = [
    # English
    re.compile(r"\b(weather|temperature|humidity|is\s+it\s+(rain|cold|hot|warm))", re.I),
    re.compile(r"\b(how'?s|what'?s)\s+(the\s+)?weather", re.I),
    re.compile(r"\b(current|right\s+now)\s+(weather|temp)", re.I),
    # Chinese (Traditional + Simplified) + Cantonese
    re.compile(r"(天氣|天气|氣溫|气温|幾多度|多少度|熱唔熱|热不热|冷唔冷|冷不冷)"),
    re.compile(r"(而家|依家|現在|现在|今日)(嘅|的)?\s*(天氣|天气|氣溫|气温)"),
    # Japanese
    re.compile(r"(天気|気温|暑い|寒い)"),
    # Korean
    re.compile(r"(날씨|기온)"),
]

_AQI_PATTERNS = [
    re.compile(r"\b(aqi|aqhi|air\s+quality|pollut(ion|ed)|pm2\.?5|pm10|smog)", re.I),
    re.compile(r"(空氣質素|空气质量|空氣品質|空气品质|AQHI|污染)"),
    re.compile(r"(空気の質|空気汚染|대기질|미세먼지)"),
]

_WARNINGS_PATTERNS = [
    re.compile(r"\b(typhoon|storm|rainstorm|landslip|warning|signal)\b", re.I),
    re.compile(r"(颱風|台风|暴雨|雷暴|山泥傾瀉|警告|訊號|信号|T8|黑雨|紅雨|红雨)"),
    re.compile(r"(台風|警報|주의보|경보)"),
]

# Combined weather + air (for "should I go outside" style questions).
_COMBINED_PATTERNS = [
    re.compile(r"\b(go\s+outside|outdoors?|outdoor\s+activity|outdoor\s+exercise)", re.I),
    re.compile(r"(出街|出去|出門|出门|戶外|户外)"),
]

# Chitchat / pleasantries — return a canned short reply, no tool call.
_CHITCHAT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"^\s*(hi|hello|hey)[!. ]*$", re.I),
        "Hi — ask me about HK transport, weather, air quality, courts, pools, or housing.",
    ),
    (
        re.compile(r"^\s*(你好|您好)\s*[!。.]*$"),
        "你好。可以問我香港嘅交通、天氣、空氣質素、球場、泳池或者公屋。",
    ),
    (
        re.compile(r"^\s*(哈囉|哈罗|喂)\s*[!。.]*$"),
        "哈囉！想知香港嘅交通、天氣、空氣、球場、泳池定公屋資料？",
    ),
    (re.compile(r"^\s*(唔該|多謝|謝謝|谢谢|ありがとう|감사)", re.I), "唔使客氣。"),
    (re.compile(r"^\s*(thanks?|thank\s+you)[!. ]*$", re.I), "You're welcome."),
]


def classify(text: str) -> FastPathHit | None:
    """Return a FastPathHit when the text maps cleanly to a trivial intent."""
    t = text.strip()
    if not t:
        return None

    # Chitchat — exact-ish matches only, never on long utterances.
    if len(t) <= 40:
        for pat, reply in _CHITCHAT_PATTERNS:
            if pat.search(t):
                return FastPathHit(intent="chitchat", tools=[], reply_if_chitchat=reply)

    has_weather = any(p.search(t) for p in _WEATHER_PATTERNS)
    has_aqi = any(p.search(t) for p in _AQI_PATTERNS)
    has_warnings = any(p.search(t) for p in _WARNINGS_PATTERNS)
    has_outdoor = any(p.search(t) for p in _COMBINED_PATTERNS)

    # Hybrid: mentions outdoor activity → weather + aqi + warnings together.
    if has_outdoor:
        tools = ["context.get_current_weather", "context.get_aqhi", "context.get_active_warnings"]
        return FastPathHit(intent="weather_and_air", tools=tools)

    # Disqualify combinations that need routing (destination + weather etc.).
    # Bail on any cross-intent mix — let the LLM handle it.
    domain_flags = sum(1 for f in (has_weather, has_aqi, has_warnings) if f)
    if domain_flags > 1:
        # e.g. "weather + warnings" is fine as combined context — merge.
        tools = []
        if has_weather:
            tools.append("context.get_current_weather")
        if has_warnings:
            tools.append("context.get_active_warnings")
        if has_aqi:
            tools.append("context.get_aqhi")
        return FastPathHit(intent="weather_and_air", tools=tools)

    # Disqualify weather queries that name a non-HK location or a specific day.
    # (HKO rhrread is always HK-wide and "right now"; other intents need the LLM.)
    if has_weather:
        if _mentions_future(t) or _mentions_non_hk(t):
            return None
        return FastPathHit(intent="weather", tools=["context.get_current_weather"])
    if has_aqi:
        return FastPathHit(intent="aqi", tools=["context.get_aqhi"])
    if has_warnings:
        return FastPathHit(intent="warnings", tools=["context.get_active_warnings"])

    return None


_FUTURE_PATTERNS = [
    re.compile(r"\b(tomorrow|next\s+(week|weekend)|weekend|tonight|later)\b", re.I),
    re.compile(r"(聽日|明日|明天|下星期|週末|周末|今晚)"),
]

_NON_HK_PATTERNS = [
    re.compile(
        r"\b(in|at)\s+(tokyo|osaka|seoul|beijing|shanghai|taipei|bangkok|singapore)\b", re.I
    ),
    re.compile(r"(東京|大阪|首爾|首尔|北京|上海|台北|曼谷|新加坡)"),
]


def _mentions_future(text: str) -> bool:
    return any(p.search(text) for p in _FUTURE_PATTERNS)


def _mentions_non_hk(text: str) -> bool:
    return any(p.search(text) for p in _NON_HK_PATTERNS)
