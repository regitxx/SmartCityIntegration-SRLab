# ruff: noqa: RUF001, RUF003  # CJK fullwidth punctuation is intentional (replies + comments).
"""Deterministic pre-classifier for trivial + POI-find intents.

When a user's turn is unambiguously "what's the weather", "what's the AQI",
"any warnings", pure chitchat ("hi", "thanks"), or a clean POI-find
("supermarket near Mong Kok", "美孚附近有冇廁所"), we can skip the first LLM
hop entirely — call the tool(s) directly and synthesise the final message in
one LLM call instead of two. That halves latency for the most common intents.

Rules are deliberately narrow. If a pattern is ambiguous (multiple POI
categories, a POI term mixed with weather, no extractable location), we
defer to the full LLM path. Classifier confidence is binary: either we have
a clear fast-path intent or we don't.

HK scoping is structural, not list-based: this module never enumerates
foreign place names. A located weather/air question defers to the LLM (HKO
data is territory-wide; "weather in <place>" needs judgement). A POI
location phrase is extracted by *mechanism* (closed-class markers + function
-word stripping) and validated downstream against the government ALS lookup
— the API is the gazetteer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from smcity.tools.poi_categories import categorize_all, strip_category_terms

FastPathIntent = Literal["weather", "aqi", "warnings", "weather_and_air", "chitchat", "poi"]


@dataclass(slots=True)
class FastPathHit:
    intent: FastPathIntent
    tools: list[str] = field(default_factory=list)  # tools to call directly (chitchat/poi: [])
    reply_if_chitchat: str | None = None  # when intent == chitchat
    poi_category: str | None = None  # when intent == poi — registry slug
    location: str | None = None  # extracted phrase; orchestrator resolves via geo.address_lookup
    location_is_self: bool = False  # "near me" / bare 附近 — use the session's user_location


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
# Every row is anchored to a FULL match (modulo trailing punctuation): a
# prefix match would swallow real requests ("唔該，附近有冇廁所？" is
# "excuse me, any toilets nearby?", not thanks). Replies are per-language —
# this path has no synthesis hop, so the language invariant must hold here.
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
    (
        re.compile(r"^\s*(唔該(?:晒|曬)?|多謝(?:晒|曬)?|謝謝|谢谢)\s*[!！。.~]*$"),
        "唔使客氣。",
    ),
    (
        re.compile(r"^\s*(ありがとう(?:ございます|ございました)?|どうも)\s*[!！。.~]*$"),
        "どういたしまして。",
    ),
    (
        re.compile(r"^\s*(감사합니다|감사해요|고맙습니다|고마워요?)\s*[!！。.~]*$"),
        "천만에요.",
    ),
    (re.compile(r"^\s*(thanks?|thank\s+you)[!. ]*$", re.I), "You're welcome."),
]


# --- location extraction ----------------------------------------------------
# Mechanism, not knowledge. A "location phrase" is whatever wraps a
# closed-class marker (附近 / 喺 / 有冇 / near / in / at …) once closed-class
# function words are stripped from the edges. Whether the phrase is a real HK
# place is decided downstream by the government ALS lookup — the API is the
# gazetteer; no place-name list lives in code.

LocationKind = Literal["none", "self", "hk", "phrase"]

_CJK = "㐀-鿿"

# Cantonese/Chinese word order: location PRECEDES the marker (美孚附近, 旺角有冇).
_CJK_NEAR_RE = re.compile(
    rf"([{_CJK}]{{0,12}})(?:附近|左近|周圍|周围|周邊|周边|一帶|一带|旁邊|旁边)"
)
_CJK_EXIST_RE = re.compile(
    rf"([{_CJK}]{{0,12}})(?:邊度有|边度有|哪度有|哪裏有|哪裡有|哪里有|有冇|有没有)"
)
# …and FOLLOWS the locative verb/preposition (喺旺角, 在中環). The lookbehind
# keeps 在-compounds (現在/正在/實在/存在) from faking a locative.
_CJK_AT_RE = re.compile(rf"(?:喺|响|響|(?<![現现實实正存好])在|於|于)\s*([{_CJK}]{{1,12}})")

_CJK_SELF_TERMS = ("呢度", "嗰度", "依度", "這裡", "这里", "這邊", "这边", "身邊", "身边")

# Closed-class function words stripped off phrase edges (longest-first).
_CJK_LEFT_STRIP = tuple(
    sorted(
        (
            "我哋",
            "我们",
            "你哋",
            "你们",
            "我",
            "你",
            "佢",
            "想搵",
            "想揾",
            "想找",
            "想要",
            "想",
            "要",
            "請問",
            "请问",
            "唔該",
            "唔该",
            "麻煩",
            "麻烦",
            "幫我",
            "帮我",
            "同我",
            "畀我",
            "给我",
            "而家",
            "依家",
            "現在",
            "现在",
            "今日",
            "今天",
            "今晚",
            "目前",
            "喺",
            "响",
            "響",
            "在",
            "於",
            "于",
            "去",
            "嘅",
            "的",
            "有冇",
            "有没有",
            "有",
            "搵",
            "揾",
            "找",
            "離",
            "离",
            "近",
            "從",
            "从",
            "請列出",
            "请列出",
            "請顯示",
            "请显示",
            "列出",
            "顯示",
            "显示",
        ),
        key=len,
        reverse=True,
    )
)
_CJK_RIGHT_STRIP = tuple(
    sorted(
        (
            "有冇",
            "有没有",
            "有",
            "搵",
            "揾",
            "找",
            "買",
            "买",
            "食",
            "飲",
            "饮",
            "嘅",
            "的",
            "啲",
            "個",
            "个",
            "間",
            "间",
            "邊",
            "边",
            "哪",
            "度",
            "呀",
            "啊",
            "嗎",
            "吗",
            "呢",
            "啦",
            "喇",
            "哪裡",
            "哪裏",
            "哪里",
            "哪兒",
            "哪儿",
            "邊度",
            "嗰邊",
            "呢邊",
            "地區",
            "地区",
            "市中心",
            "社區",
            "社区",
            "呢區",
            "這區",
            "那區",
            "嗰區",
            "這個",
            "这个",
            "呢個",
            "嗰個",
            "那個",
            "那个",
            "工作",
            "返工",
            "住",
            "近",
            "內",
            "内",
            "入面",
            "出嚟",
            "出来",
            "出來",
            "尋找",
            "寻找",
            "尋",
            "寻",
        ),
        key=len,
        reverse=True,
    )
)

_EN_SELF_RE = re.compile(
    r"(?<![a-zA-Z])(?:near\s*by|close\s+by|(?:near|around|by)\s+(?:me|here|us)|right\s+here)"
    r"(?![a-zA-Z])",
    re.I,
)
_EN_PHRASE_RE = re.compile(
    r"(?<![a-zA-Z])(?:near|around|at|in|beside|close\s+to|next\s+to)\s+(?:the\s+)?"
    r"([a-zA-Z][a-zA-Z0-9'\- ]{1,40}?)\s*(?=[?!.,;:！？。，]|$)",
    re.I,
)
_EN_TRAIL_STOP = frozenset({"please", "thanks", "thank", "you", "now", "today", "tonight"})

# The service's own territory — territory-wide scope, not a point location.
# (HK itself is the deployment domain, not a foreign-place list.)
_HK_TERMS_RE = re.compile(r"^(?:hk|hong\s*kong|香港|全港)$", re.I)


def _strip_edges(s: str) -> str:
    changed = True
    while changed and s:
        changed = False
        for tok in _CJK_LEFT_STRIP:
            if s.startswith(tok):
                s = s[len(tok) :]
                changed = True
        for tok in _CJK_RIGHT_STRIP:
            if s.endswith(tok):
                s = s[: -len(tok)]
                changed = True
    return s


_CJK_INNER_LOCATIVE_RE = re.compile(r"[喺响響在於于]")


def _classify_cjk_phrase(raw: str) -> tuple[LocationKind, str | None]:
    # A locative verb INSIDE the capture window ("請列出吓喺屯門區" before 附近)
    # means the real place starts after it — keep only that tail.
    parts = _CJK_INNER_LOCATIVE_RE.split(raw)
    if len(parts) > 1:
        raw = parts[-1]
    phrase = _strip_edges(raw)
    if not phrase or phrase in _CJK_SELF_TERMS:
        return ("self", None)
    if _HK_TERMS_RE.match(phrase):
        return ("hk", None)
    return ("phrase", phrase)


def extract_location(text: str) -> tuple[LocationKind, str | None]:
    """Pull a location signal out of (category-stripped) text.

    Returns (kind, phrase): "self" for near-me markers, "hk" for the
    territory itself ("in HK", 香港 — not a point), "phrase" for a candidate
    place name to be validated by ALS, else ("none", None).
    """
    m = _CJK_NEAR_RE.search(text)
    if m:
        return _classify_cjk_phrase(m.group(1))
    if any(term in text for term in _CJK_SELF_TERMS):
        return ("self", None)
    m = _CJK_EXIST_RE.search(text)
    if m:
        return _classify_cjk_phrase(m.group(1))
    m = _CJK_AT_RE.search(text)
    if m:
        kind, phrase = _classify_cjk_phrase(m.group(1))
        if kind != "phrase" or phrase:
            return (kind, phrase)

    if _EN_SELF_RE.search(text):
        return ("self", None)
    matches = list(_EN_PHRASE_RE.finditer(text))
    if matches:
        toks = matches[-1].group(1).strip().split()
        while toks and toks[-1].lower() in _EN_TRAIL_STOP:
            toks.pop()
        if toks and len(toks) <= 4:
            phrase = " ".join(toks)
            if _HK_TERMS_RE.match(phrase):
                return ("hk", None)
            return ("phrase", phrase)
    return ("none", None)


# A CJK place name jammed directly onto a weather term (東京天氣 / 沙田氣溫)
# has no locative marker for `extract_location` to find — catch it here.
_CJK_WEATHER_PREFIX_RE = re.compile(rf"([{_CJK}]{{1,8}})(?:天氣|天气|氣溫|气温)")


def _named_location_present(text: str) -> bool:
    """True when the text names a specific place (deferral signal for the
    weather/air/warnings fast path — HKO/EPD data is territory-wide)."""
    kind, _ = extract_location(text)
    if kind == "phrase":
        return True
    m = _CJK_WEATHER_PREFIX_RE.search(text)
    if m:
        prefix = _strip_edges(m.group(1))
        return bool(prefix) and not _HK_TERMS_RE.match(prefix)
    return False


# A routing-shaped question ("how do I get to the supermarket near Mei Foo",
# 點樣去…) names a POI category but wants directions — never a POI list.
_ROUTING_PATTERNS = [
    re.compile(
        r"\b(how\s+(do|can)\s+i\s+get|get\s+to|route|directions?|take\s+me"
        r"|bus\s+to|mtr\s+to|train\s+to|walk\s+to|on\s+the\s+way)\b",
        re.I,
    ),
    re.compile(
        r"(點樣去|點去|怎麼去|怎么去|帶我去|带我去|搭咩車|坐咩車|搭乜嘢車|坐什么车|路線|路线|幾號巴士|几号巴士)"
    ),
]


def _mentions_routing(text: str) -> bool:
    return any(p.search(text) for p in _ROUTING_PATTERNS)


# --- classifier -------------------------------------------------------------

# POI fast-path length guard: long utterances carry qualifiers ("open late",
# "cheapest", multi-step plans) that need LLM judgement.
_POI_MAX_LEN = 120


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
    has_context = has_weather or has_aqi or has_warnings or has_outdoor

    # POI-find: exactly one registry category + an extractable location.
    poi_slugs = categorize_all(t) if len(t) <= _POI_MAX_LEN else ()
    if poi_slugs:
        if has_context or len(poi_slugs) > 1:
            return None  # cross-domain mix or ambiguous category — LLM decides
        if _mentions_routing(t) or _mentions_future(t):
            return None  # directions / time-qualified — LLM decides
        slug = poi_slugs[0]
        kind, phrase = extract_location(strip_category_terms(t, slug))
        if kind == "phrase":
            return FastPathHit(intent="poi", poi_category=slug, location=phrase)
        if kind == "self":
            return FastPathHit(intent="poi", poi_category=slug, location_is_self=True)
        return None  # no location signal / territory-wide — LLM decides

    # Located weather/air questions defer: the data is territory-wide and
    # judging whether a named place is in scope is the LLM's job.
    if has_context and _named_location_present(t):
        return None

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

    # Disqualify weather queries about a specific day — HKO rhrread is
    # "right now"; forecasts need the LLM path.
    if has_weather:
        if _mentions_future(t):
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


def _mentions_future(text: str) -> bool:
    return any(p.search(text) for p in _FUTURE_PATTERNS)
