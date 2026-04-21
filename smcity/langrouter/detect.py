"""Language detection — particle heuristic + unicode-script majority.

Strategy (cheap → expensive):

1. **Particle heuristic** — if any strong Cantonese particle (嘅/喺/咗/冇/佢/…)
   is present, classify as `yue` with 0.92 confidence. Near-zero false positives
   in a HK smart-city context.
2. **Script majority** — count codepoints by unicode block (Han, Hiragana, Hangul,
   Thai, Arabic, Cyrillic, Hebrew, Latin) and pick the dominant script.
3. **Simplified vs Traditional** — for Han-dominant text that didn't hit the
   Cantonese path, check for simplified-exclusive characters to decide.
4. **Code-switching** — emit a flag when Latin words coexist with CJK text; the
   response formatter keeps the same flag on the response.

This is deterministic, dependency-free, and fast (< 1 ms on typical queries).
Phase 2 replaces step 2 with fastText + HIT-TMG/LID-HK.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal

# --- Cantonese particles --------------------------------------------------
# Deliberately narrow — grammatical particles that standard written Mandarin
# essentially never uses. False positives must stay near zero.
CANTONESE_PARTICLES: frozenset[str] = frozenset(
    {
        "嘅",  # ge3 — possessive / nominaliser
        "喺",  # hai2 — locative existential "at"
        "咗",  # zo2 — perfective aspect
        "冇",  # mou5 — "doesn't have"
        "佢",  # keoi5 — 3sg pronoun
        "乜",  # mat1 — interrogative "what"
        "咁",  # gam2 / gam3 — "so / like that"
        "唔",  # m4 — negation prefix ("唔係", "唔知")
        "㗎",  # gaa3 — emphatic particle
        "喎",  # wo3 — reportative particle
        "囉",  # lo3 — assertive particle
        "喇",  # laa3 — change-of-state particle
        "咋",  # zaa3 — restrictive particle
        "啦",  # laa1 — softener particle
        "啫",  # ze1 — "only"
        "嘞",  # laak3 — assertive
        "咩",  # me1 — "what / ya know?"
        "係咪",  # hai6 mai6 — A/B question frame
        "嗰",  # go2 — distal demonstrative "that"
    }
)

# Two-character Cantonese-specific sequences that carry extra signal.
CANTONESE_BIGRAMS: frozenset[str] = frozenset(
    {
        "點樣",  # how
        "乜嘢",  # what
        "邊度",  # where
        "邊個",  # who/which
        "幾多",  # how much
        "唔該",  # please / thanks
        "幾時",  # when
        "依家",  # now (spoken/written Cantonese variant of 而家)
        "而家",  # now (colloquial)
        "點呀",  # how is it?
        "點先",  # how does one...
        "啲嘢",  # stuff
    }
)

# --- Simplified-exclusive characters --------------------------------------
# A small, high-signal set. Not exhaustive — we only need enough coverage to
# flip the verdict when clearly simplified text slips through.
SIMPLIFIED_EXCLUSIVE: frozenset[str] = frozenset(
    "国时间从这没说话还实际应给让现头队长业点众发书产东车间机办"
    "务电会经产难题队图书级见买认达写运风话议际发觉"
    "么环将带义汉办龙终继绕绪纪线级纳纲纸纽纷纹纯练组细织经绝"
    "车东两专业丛东丝丢两严丧丽举乌习买争买亘亚亲亿仅仑仓"
    "传伟伤伦伪体佣侠侦侪俨俦倾偿傥傧储儿兰关兴兹养兽冻凉"
    "凌处几"
)

# Script classifiers — unicode-range based.
_SCRIPT_RANGES: list[tuple[str, tuple[int, int]]] = [
    ("Han", (0x4E00, 0x9FFF)),
    ("Han", (0x3400, 0x4DBF)),  # CJK Ext-A
    ("Han", (0x20000, 0x2A6DF)),  # CJK Ext-B
    ("Hiragana", (0x3040, 0x309F)),
    ("Katakana", (0x30A0, 0x30FF)),
    ("Hangul", (0xAC00, 0xD7AF)),
    ("Hangul", (0x1100, 0x11FF)),  # Jamo
    ("Thai", (0x0E00, 0x0E7F)),
    ("Arabic", (0x0600, 0x06FF)),
    ("Hebrew", (0x0590, 0x05FF)),
    ("Cyrillic", (0x0400, 0x04FF)),
    ("Devanagari", (0x0900, 0x097F)),
    ("Greek", (0x0370, 0x03FF)),
]

_ASCII_LATIN_WORD = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]{2,}")

# Script → ISO 639-1/3 primary_lang (best guess; may be refined by caller).
SCRIPT_PRIMARY: dict[str, str] = {
    "Han": "zho",  # override to yue by particle heuristic or zh-Hant/Hans by script variant
    "Hiragana": "jpn",
    "Katakana": "jpn",
    "Hangul": "kor",
    "Thai": "tha",
    "Arabic": "ara",
    "Hebrew": "heb",
    "Cyrillic": "rus",
    "Devanagari": "hin",
    "Greek": "ell",
    "Latin": "und",  # under-determined; caller can ask the LLM to disambiguate
}


@dataclass(slots=True)
class LangDetection:
    primary_lang: str
    script: Literal[
        "Hant",
        "Hans",
        "Latin",
        "Hiragana",
        "Katakana",
        "Hangul",
        "Thai",
        "Arabic",
        "Hebrew",
        "Cyrillic",
        "Devanagari",
        "Greek",
        "Mixed",
        "Other",
    ]
    is_code_switched: bool = False
    code_switch_langs: list[str] = field(default_factory=list)
    confidence: float = 0.0
    method: Literal["particle", "script", "carried", "forced"] = "script"
    tts_locale: str = "en-US"  # best-guess BCP-47 locale for a downstream TTS
    secondary_langs: list[str] = field(default_factory=list)

    @property
    def is_cantonese(self) -> bool:
        return self.primary_lang == "yue"


# --- scorers --------------------------------------------------------------


def _script_counts(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for ch in text:
        cp = ord(ch)
        if ch.isspace() or unicodedata.category(ch).startswith(("P", "S", "N")):
            continue
        matched = False
        for name, (lo, hi) in _SCRIPT_RANGES:
            if lo <= cp <= hi:
                counts[name] = counts.get(name, 0) + 1
                matched = True
                break
        if not matched and "a" <= ch.lower() <= "z":
            counts["Latin"] = counts.get("Latin", 0) + 1
    return counts


def _dominant_script(counts: dict[str, int]) -> tuple[str, int]:
    if not counts:
        return "Other", 0
    script, n = max(counts.items(), key=lambda kv: kv[1])
    return script, n


def _has_cantonese_particle(text: str) -> bool:
    if any(p in text for p in CANTONESE_PARTICLES):
        return True
    return any(bg in text for bg in CANTONESE_BIGRAMS)


def _is_simplified(text: str) -> bool:
    return any(ch in SIMPLIFIED_EXCLUSIVE for ch in text)


def _tts_locale_for(primary_lang: str, script: str) -> str:
    if primary_lang == "yue":
        return "yue-HK"
    if primary_lang == "zho":
        return "zh-HK" if script == "Hant" else "zh-CN"
    return {
        "jpn": "ja-JP",
        "kor": "ko-KR",
        "tha": "th-TH",
        "ara": "ar-EG",
        "heb": "he-IL",
        "rus": "ru-RU",
        "hin": "hi-IN",
        "ell": "el-GR",
        "eng": "en-US",
    }.get(primary_lang, "en-US")


def detect(text: str, *, carried: LangDetection | None = None) -> LangDetection:
    """Detect the dominant language of `text`.

    `carried` — the session's previous LangDetection, used only to break ties on
    single-word queries where the current text is ambiguous (e.g. place names).
    """
    if not text.strip():
        return carried or LangDetection(
            primary_lang="und", script="Other", confidence=0.0, method="script"
        )

    counts = _script_counts(text)
    latin_hits = bool(_ASCII_LATIN_WORD.search(text))
    script, _ = _dominant_script(counts)
    kana_count = counts.get("Hiragana", 0) + counts.get("Katakana", 0)
    han_count = counts.get("Han", 0)

    # 1) Cantonese particle hit — authoritative
    if _has_cantonese_particle(text):
        code_switch = latin_hits and counts.get("Latin", 0) > 0
        return LangDetection(
            primary_lang="yue",
            script="Hant",
            is_code_switched=code_switch,
            code_switch_langs=["yue", "eng"] if code_switch else ["yue"],
            confidence=0.92,
            method="particle",
            tts_locale="yue-HK",
            secondary_langs=["eng"] if code_switch else [],
        )

    # 2) Any kana present → Japanese (Chinese text never has kana).
    if kana_count > 0:
        return LangDetection(
            primary_lang="jpn",
            script="Hiragana"
            if counts.get("Hiragana", 0) >= counts.get("Katakana", 0)
            else "Katakana",
            confidence=0.9,
            method="script",
            tts_locale="ja-JP",
        )

    # 3) Han present → Chinese. Han outranks Latin even when Latin has more
    # individual characters — a HK query with a few place names in English is
    # still a Chinese query.
    if han_count > 0:
        is_simp = _is_simplified(text)
        variant: Literal["Hans", "Hant"] = "Hans" if is_simp else "Hant"
        code_switch = latin_hits and counts.get("Latin", 0) > 0
        return LangDetection(
            primary_lang="zho",
            script=variant,
            is_code_switched=code_switch,
            code_switch_langs=["zho", "eng"] if code_switch else ["zho"],
            confidence=0.8,
            method="script",
            tts_locale=_tts_locale_for("zho", variant),
            secondary_langs=["eng"] if code_switch else [],
        )

    if script == "Hangul":
        return LangDetection(
            primary_lang="kor",
            script="Hangul",
            confidence=0.92,
            method="script",
            tts_locale="ko-KR",
        )

    if script in {"Thai", "Arabic", "Hebrew", "Cyrillic", "Devanagari", "Greek"}:
        lang = SCRIPT_PRIMARY[script]
        return LangDetection(
            primary_lang=lang,
            script=script,  # type: ignore[arg-type]
            confidence=0.9,
            method="script",
            tts_locale=_tts_locale_for(lang, script),
        )

    # 3) Latin-script fallback
    if counts.get("Latin", 0) > 0 or latin_hits:
        # Under-determined — the orchestrator may ask the LLM to refine.
        # For Phase 1a we default to English; the coverage matrix flags `und`
        # as "ask the LLM which European language it is" downstream if needed.
        return LangDetection(
            primary_lang="eng",
            script="Latin",
            confidence=0.55,
            method="script",
            tts_locale="en-US",
            secondary_langs=["und"],
        )

    return LangDetection(
        primary_lang=carried.primary_lang if carried else "und",
        script="Other",
        confidence=0.1,
        method="carried" if carried else "script",
        tts_locale=carried.tts_locale if carried else "en-US",
    )
