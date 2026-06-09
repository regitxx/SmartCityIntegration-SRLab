# CJK glyphs in regex + prose are intentional.
"""Post-synthesis invariant checks for the orchestrator.

The LLM sometimes fires a tool, gets non-empty results, then writes
"I couldn't find any" in the final reply — denying its own tool data.
We catch that structurally after synthesis: if a successful tool returned
records AND the reply contains denial language AND the reply doesn't mention
any of the returned records, we treat it as a violation and re-prompt the
LLM with the records pre-quoted.

The engine is declarative: each `SynthesisInvariant` is (name, check_fn).
Add new invariants by appending to `DEFAULT_INVARIANTS`.

This is the synthesis-side counterpart to `smcity/chain_rules.py`:
- chain_rules runs BEFORE synthesis, on the tool-call list.
- synthesis_invariants runs AFTER synthesis, on the reply text.

Both share the philosophy: structural enforcement in code beats hoping the
prompt convinces the LLM to behave.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from smcity.langrouter.detect import LangDetection
from smcity.tools.registry import ToolResult

# --- violation + invariant types ------------------------------------------


@dataclass(frozen=True, slots=True)
class InvariantViolation:
    """A reply that failed an invariant check.

    `corrective_prompt` is a system message the orchestrator can append to
    the message history before re-prompting the LLM. It explains the
    violation and tells the LLM exactly what to produce instead.
    """

    name: str  # invariant name, for telemetry
    kind: str  # category slug, e.g. "data_denial"
    corrective_prompt: str
    # Optional metadata for logging — what tool fired, how many records.
    tool_name: str | None = None
    record_count: int = 0


@dataclass(frozen=True, slots=True)
class SynthesisInvariant:
    """A single check run against the LLM's reply post-synthesis."""

    name: str
    check: Callable[
        [str, list[ToolResult], LangDetection],
        InvariantViolation | None,
    ]


# --- engine ---------------------------------------------------------------


def apply_invariants(
    reply: str,
    tool_results: list[ToolResult],
    detection: LangDetection,
    invariants: Iterable[SynthesisInvariant] | None = None,
) -> InvariantViolation | None:
    """Run each invariant in order; return the first violation, or None.

    Invariants are evaluated in registration order — earlier ones take
    precedence. Currently we only have one (data_denial); the engine is
    set up to add more without orchestrator changes.
    """
    if invariants is None:
        invariants = DEFAULT_INVARIANTS
    if not reply or not reply.strip():
        return None  # empty reply is a different problem; handled elsewhere
    for inv in invariants:
        violation = inv.check(reply, tool_results, detection)
        if violation is not None:
            return violation
    return None


# --- denial-pattern regex (multilingual) ----------------------------------
#
# Covers all languages the agent serves. False negatives (missing a phrasing
# in some language) just mean we leak the bad reply through — cost is bounded.
# False positives (flagging a legitimate hedge) cost one re-prompt. Bias
# toward strict patterns: full phrases, not single words.

_DENIAL_PATTERN = re.compile(
    r"""
    # --- English -------------------------------------------------------
    \bi\s+(?:could\s*not|couldn'?t|can'?t|cannot)\s+find\b
    | \bi\s+(?:was|am)\s+unable\s+to\s+(?:find|locate)\b
    | \bi\s+don'?t\s+have\s+(?:that|any|specific|the|info|information|data)\b
    | \bi\s+(?:do\s+not|don'?t)\s+have\s+info(?:rmation)?\b
    | \bno\s+(?:results?|data|info(?:rmation)?|matches?)\s+(?:found|available)?\b
    | \bunable\s+to\s+(?:find|locate)\b
    | \bno\s+such\s+(?:place|location|venue|stop|station)\b
    | \bsorry,?\s+(?:i\s+)?(?:could\s*not|couldn'?t|can'?t)\s+find\b

    # --- Cantonese / Traditional Chinese -------------------------------
    | 搵唔到 | 揾唔到 | 找不到 | 我冇.*資料 | 沒有.*資料 | 沒有相關
    | 無相關 | 唔知 | 唔清楚 | 對唔住.*搵唔到

    # --- Simplified Chinese --------------------------------------------
    | 找不到 | 没有.*资料 | 没有相关 | 无相关 | 我不知道 | 我没有.*信息
    | 抱歉.*没有 | 抱歉.*找不到

    # --- Japanese ------------------------------------------------------
    | 見つかりません | 見つかりませんでした | 情報がありません
    | 申し訳ありません.*見つかり

    # --- Korean --------------------------------------------------------
    | 찾을\s*수\s*없 | 정보가\s*없 | 죄송합니다.*찾을\s*수

    # --- French --------------------------------------------------------
    | je\s+n'?ai\s+pas\s+trouv | aucun\s+résultat | pas\s+de\s+(?:données|résultats)
    | désolé,?\s+je\s+n'?ai\s+pas

    # --- German --------------------------------------------------------
    | nicht\s+gefunden | keine\s+(?:ergebnisse|daten|informationen|treffer)
    | leider.*nicht\s+gefunden

    # --- Spanish -------------------------------------------------------
    | no\s+(?:encontré|encontre)
    | no\s+hay\s+resultados
    | sin\s+resultados
    | lo\s+siento,?\s+no\s+(?:encontré|encontre)

    # --- Thai ----------------------------------------------------------
    | ไม่พบ | ไม่มีข้อมูล

    # --- Vietnamese ----------------------------------------------------
    | không\s+tìm\s+thấy | không\s+có\s+(?:dữ\s+liệu|thông\s+tin)

    # --- Indonesian / Malay --------------------------------------------
    | tidak\s+ditemukan | tidak\s+ada\s+(?:data|hasil|informasi)

    # --- Tagalog -------------------------------------------------------
    | hindi\s+(?:mahanap|nakita) | walang\s+(?:resulta|impormasyon)
    """,
    re.IGNORECASE | re.VERBOSE | re.UNICODE,
)


# --- record-name extraction (generic) -------------------------------------
#
# Tools return dicts where the records live in some list-shaped field.
# Different tools call it different things: `pois`, `etas`, `stops`,
# `courts`, `candidates`. Rather than hard-coding a per-tool extractor map
# (which violates the global-mechanism principle), we scan the result for
# any list-of-dicts and pull common name-shaped fields off each item.

_NAME_KEYS: tuple[str, ...] = (
    "name_en",
    "name",
    "name_tc",
    "name_zh",
    "name_sc",
    "station_name_en",
    "station_name_tc",
    "stop_name_en",
    "stop_name_tc",
    "destination_en",
    "destination_tc",
    "route",  # for ETA records
    "address",
    "address_en",
)


def _extract_record_names(result: dict[str, Any] | None, limit: int = 5) -> list[str]:
    """Return up to `limit` name-like strings from list-shaped result fields.

    Generic over all tools: scans every top-level list, looks at each item's
    common name keys, and collects the first one it finds. Returns [] for
    empty results or non-dict input.
    """
    if not isinstance(result, dict):
        return []
    names: list[str] = []
    for value in result.values():
        if len(names) >= limit:
            break
        if not isinstance(value, list) or not value:
            continue
        for item in value:
            if len(names) >= limit:
                break
            if not isinstance(item, dict):
                continue
            for key in _NAME_KEYS:
                candidate = item.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    names.append(candidate.strip())
                    break
    return names


def _reply_mentions_any(reply: str, names: list[str]) -> bool:
    """Case-insensitive substring check — does the reply include any name?"""
    if not names:
        return False
    low = reply.lower()
    return any(name.lower() in low for name in names if name)


# --- data_denial invariant ------------------------------------------------


def _data_denial_check(
    reply: str,
    tool_results: list[ToolResult],
    detection: LangDetection,
) -> InvariantViolation | None:
    """Fire when a successful tool returned non-empty records BUT the reply
    denies data AND doesn't mention any of those records.

    Three guards keep false-positive rate low:
    1. At least one tool result must be ok-status with non-empty records.
    2. The reply must contain explicit denial language (multilingual regex).
    3. The reply must NOT mention any record name — if even one slips in,
       the LLM is honoring the data and we leave it alone.
    """
    if not _DENIAL_PATTERN.search(reply):
        return None  # no denial language → no violation possible

    # Find the first tool with non-empty records. We surface that one
    # specifically in the corrective prompt so the LLM knows what to cite.
    for result in tool_results:
        if result.status != "ok":
            continue
        names = _extract_record_names(result.result, limit=5)
        if not names:
            continue
        if _reply_mentions_any(reply, names):
            return None  # LLM IS citing the data — no violation
        return InvariantViolation(
            name="data_denial",
            kind="data_denial",
            tool_name=result.name,
            record_count=len(names),
            corrective_prompt=_build_data_denial_prompt(result.name, names, detection),
        )
    return None


def _build_data_denial_prompt(
    tool_name: str,
    names: list[str],
    detection: LangDetection,
) -> str:
    sample = ", ".join(names[:5])
    return (
        f"Your previous reply denied having data, but tool `{tool_name}` "
        f"returned {len(names)} non-empty record(s) (e.g. {sample}). "
        "Rewrite the reply to PRESENT those records to the user. Do NOT "
        "write 'I couldn't find', 'no data', '搵唔到', '找不到', or any "
        "equivalent denial — the tool returned data, you must use it. Cite "
        f"at least the first record by name. Reply in {detection.primary_lang!r} "
        f"({detection.tts_locale})."
    )


DATA_DENIAL_INVARIANT = SynthesisInvariant(
    name="data_denial",
    check=_data_denial_check,
)


# --- registered invariants ------------------------------------------------
#
# Add new invariants here. Order matters — earlier invariants fire first
# when multiple would apply (the engine returns on first match).

# --- wrong_language invariant ---------------------------------------------
#
# Calibrated v0.6.3 fuzz showed ~18% of replies in a language different
# from the user's question — and the rate is stable across "biased" and
# "calibrated" runs, so this is genuine agent behaviour, not a judge
# artifact. The `language_stick_reminder` system message already exists
# in `smcity/prompts.py` but apparently isn't strong enough — gpt-oss-120b
# drifts to English on Chinese queries especially when tool results
# contain English-named records.
#
# Structural fix per the project's "enforcement > prompt instruction"
# principle: detect the mismatch post-synthesis via character-class
# heuristics (cheap, robust, no extra model call) and re-prompt the LLM
# with a corrective hint that includes a sentence-starter in the target
# language.

# Detection rule, in plain language: classify each reply as "looks-CJK",
# "looks-Latin", or "mixed/inconclusive" by the ratio of script-classified
# code points. A reply is `wrong_language` when:
#   - user is Chinese-script (yue / zho-* / detection.script in {Hant, Hans})
#     AND the reply is looks-Latin
#   - user is Latin-script (eng / fra / deu / …)
#     AND the reply is looks-CJK
# "mixed" replies (e.g. "Mong Kok 旺角 has 5 stops") are tolerated — they
# match the bilingual reality of HK without inducing false positives.

# Threshold below which a script class is treated as not-dominant. 30% of
# the meaningful (non-whitespace, non-punctuation, non-digit) chars must
# carry the script for it to count.
_SCRIPT_DOMINANT_RATIO = 0.30


def _meaningful_chars(text: str) -> str:
    """Return only the characters that carry script identity.

    Strips whitespace, digits, common punctuation, and ASCII brackets so
    the ratio isn't dominated by numbers + punctuation that all replies
    share. CJK punctuation and ASCII letters / CJK ideographs survive.
    """
    return "".join(
        c
        for c in text
        if not c.isspace() and not c.isdigit() and c not in ".,;:!?()[]{}<>\"'-_+=*/\\|&^%$#@~`"
    )


def _is_cjk(c: str) -> bool:
    """True for any character in the major CJK Unified Ideograph ranges."""
    cp = ord(c)
    return (
        0x4E00 <= cp <= 0x9FFF  # CJK Unified Ideographs
        or 0x3400 <= cp <= 0x4DBF  # CJK Extension A
        or 0x20000 <= cp <= 0x2A6DF  # CJK Extension B
        or 0x3000 <= cp <= 0x303F  # CJK Symbols and Punctuation
        or 0xFF00 <= cp <= 0xFFEF  # Halfwidth and Fullwidth Forms
        or 0x3040 <= cp <= 0x309F  # Hiragana (for jpn detection)
        or 0x30A0 <= cp <= 0x30FF  # Katakana
    )


def _is_latin(c: str) -> bool:
    return c.isascii() and c.isalpha()


def _script_profile(text: str) -> tuple[float, float]:
    """Return (cjk_ratio, latin_ratio) over meaningful characters."""
    chars = _meaningful_chars(text)
    if not chars:
        return (0.0, 0.0)
    cjk = sum(1 for c in chars if _is_cjk(c))
    latin = sum(1 for c in chars if _is_latin(c))
    n = len(chars)
    return (cjk / n, latin / n)


_CJK_SCRIPT_USERS: frozenset[str] = frozenset({"yue", "zho", "jpn", "kor"})
_CJK_SCRIPT_CODES: frozenset[str] = frozenset({"Hant", "Hans", "Hiragana", "Hangul"})


# Skip language grading on replies with very few meaningful characters —
# something like "22.30, 114.17 — 5 min." classifies as ~100% Latin but
# is really "all data, no prose", so flagging it as wrong-language would
# be a false positive. 8 chars is enough to capture a short HK Cantonese
# phrase like "5 分鐘到" (5 mins) and reject pure-numeric replies.
_MIN_MEANINGFUL_CHARS = 8


def _wrong_language_check(
    reply: str,
    _tool_results: list[ToolResult],
    detection: LangDetection,
) -> InvariantViolation | None:
    """Fire when the reply's dominant script is opposite the user's.

    Conservative — only fires when the reply is CLEARLY in the wrong
    script. A reply with even ~30% characters in the user's expected
    script is treated as a (possibly clunky but valid) bilingual reply
    and passes through.
    """
    if not reply.strip():
        return None  # other invariants handle empty replies
    meaningful = _meaningful_chars(reply)
    if len(meaningful) < _MIN_MEANINGFUL_CHARS:
        return None  # too short to classify confidently
    cjk_ratio, latin_ratio = _script_profile(reply)
    if cjk_ratio == 0 and latin_ratio == 0:
        return None  # nothing meaningful to grade

    user_expects_cjk = (
        detection.primary_lang in _CJK_SCRIPT_USERS or detection.script in _CJK_SCRIPT_CODES
    )

    latin_dominant = latin_ratio >= _SCRIPT_DOMINANT_RATIO
    cjk_dominant = cjk_ratio >= _SCRIPT_DOMINANT_RATIO
    if user_expects_cjk and latin_dominant and not cjk_dominant:
        return InvariantViolation(
            name="wrong_language",
            kind="wrong_language",
            corrective_prompt=_build_wrong_language_prompt(detection, target="cjk"),
        )
    if (not user_expects_cjk) and cjk_dominant and not latin_dominant:
        return InvariantViolation(
            name="wrong_language",
            kind="wrong_language",
            corrective_prompt=_build_wrong_language_prompt(detection, target="latin"),
        )
    return None


def _build_wrong_language_prompt(detection: LangDetection, *, target: str) -> str:
    """Corrective prompt for the re-synthesis retry.

    Includes a concrete sentence-opener exemplar in the target language —
    the existing language_stick_reminder is a wordy instruction, this
    invariant fires AFTER the LLM has already ignored that instruction
    once, so we give it something more grounding to anchor on.
    """
    lang = detection.primary_lang
    if target == "cjk":
        opener_hint = ""
        if lang == "yue":
            opener_hint = (
                "Begin your reply with natural Cantonese — words like 而家 / 喺 / "
                "嘅 / 咗 / 冇 — NOT formal book Mandarin and NOT English. "
                "Example openings: '尖沙咀附近有…', '而家…', '你可以行去…'."
            )
        elif lang == "zho":
            opener_hint = (
                "Reply in Chinese characters (繁體 if user wrote Traditional, "
                "简体 if user wrote Simplified) — NOT English."
            )
        elif lang in {"jpn", "kor"}:
            opener_hint = f"Reply in {lang!r} using the appropriate script."
    else:  # target == "latin"
        opener_hint = (
            "Reply in plain English. Do NOT use Chinese characters in the "
            "prose. Place names may stay in their native script when no "
            "English form exists, but the body of the reply is English."
        )
    return (
        f"Your previous reply was in the wrong script. The user wrote in "
        f"{lang!r} (tts_locale {detection.tts_locale}), so the reply must "
        "match that language. Rewrite the SAME information using the SAME "
        f"tool results, but in {lang!r}. {opener_hint}"
    )


WRONG_LANGUAGE_INVARIANT = SynthesisInvariant(
    name="wrong_language",
    check=_wrong_language_check,
)


DEFAULT_INVARIANTS: list[SynthesisInvariant] = [
    DATA_DENIAL_INVARIANT,
    WRONG_LANGUAGE_INVARIANT,
]


__all__ = [
    "DATA_DENIAL_INVARIANT",
    "DEFAULT_INVARIANTS",
    "WRONG_LANGUAGE_INVARIANT",
    "InvariantViolation",
    "SynthesisInvariant",
    "apply_invariants",
]
