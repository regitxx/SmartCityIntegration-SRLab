"""Deterministic Cantonese register polish.

Runs AFTER the LLM has produced text in Cantonese-mode. Applies a small table
of formal-Chinese → Hong Kong-Cantonese substitutions to catch any stray
Mandarinisms the few-shot prompt missed. Safe to apply unconditionally to
text tagged `yue`: the substitutions are idiomatic HK Cantonese, not
transliterations or paraphrases.

Design rules:
- Only substitute when the formal form is unambiguous in a HK-agent context.
  E.g. `的` → `嘅` is safe; `是` → `係` is safe. Trickier cases (e.g. `在` → `喺`
  vs `在` as an aspect marker) are kept conservative.
- Preserve the rest of the text byte-for-byte (no reflow, no translation).
- Keep substitutions short so they're cheap (<1 ms for typical replies).
"""

from __future__ import annotations

import re

# Phrase-level substitutions — run FIRST so they don't get broken up by the
# character-level pass below.
_PHRASE_SUBS: list[tuple[str, str]] = [
    ("您好", "你好"),
    ("沒有", "冇"),
    ("那麼", "咁"),
    ("怎麼樣", "點樣"),
    ("怎麼", "點"),
    ("什麼", "乜嘢"),
    ("為什麼", "點解"),
    ("因為", "因為"),
    ("所以", "所以"),
    ("但是", "但係"),
    ("如果", "如果"),
    ("可以", "可以"),
    ("知道", "知"),
    ("現在", "而家"),
    ("已經", "已經"),
    ("應該", "應該"),
    ("而且", "而且"),
    ("還是", "定"),
    ("或者", "定係"),
    ("他們", "佢哋"),
    ("我們", "我哋"),
    ("你們", "你哋"),
    ("這裡", "呢度"),
    ("那裡", "嗰度"),
    ("這個", "呢個"),
    ("那個", "嗰個"),
    ("這些", "呢啲"),
    ("那些", "嗰啲"),
    ("一下", "一下"),
    ("讓我", "等我"),
    ("請稍等", "等陣"),
    ("謝謝", "多謝"),
    ("對不起", "對唔住"),
    ("不好意思", "唔好意思"),
    ("沒關係", "冇問題"),
    ("有沒有", "有冇"),
    ("是不是", "係咪"),
    ("不是", "唔係"),
    ("不會", "唔會"),
    ("不可以", "唔可以"),
    ("不知道", "唔知"),
    ("不知", "唔知"),
    ("不要", "唔好"),
    ("不用", "唔使"),
    ("不太", "唔太"),
    ("不過", "不過"),  # idem — avoid regressing a word that's natural in Cantonese
    ("不能", "唔能夠"),
    ("不可", "唔可以"),
    ("非常", "好"),
    ("一起", "一齊"),
    ("裡面", "入面"),
    ("外面", "出面"),
    ("上面", "上面"),
    ("下面", "下面"),
    ("一點", "少少"),
    ("有點", "有啲"),
    ("大概", "大約"),
    ("大約", "大約"),
    ("剛才", "頭先"),
    ("剛剛", "啱啱"),
    ("馬上", "即刻"),
    ("一會兒", "一陣"),
    ("打算", "打算"),
    ("方向為", "往"),
    ("到達", "到"),
    ("前往", "去"),
    ("大家", "大家"),
]

# Character-level substitutions for the commonest Mandarin function words.
# Applied after the phrase pass. These are single-codepoint swaps that are
# ~always correct in HK Cantonese text, but we guard a few with regex boundaries
# to avoid breaking proper nouns (e.g. 的士 must stay — guarded by lookahead).
_CHAR_SUBS: list[tuple[re.Pattern[str], str]] = [
    # 的 → 嘅, but leave 的士 (taxi) alone.
    (re.compile(r"的(?!士)"), "嘅"),
    # 是 → 係, except in 是不是 / 是否 which we handled via phrase pass;
    # also avoid 或是.
    (re.compile(r"(?<!或)是(?!不是)(?!否)"), "係"),
    # 在 → 喺, but NOT 正在 / 存在 / 現在 / 在於 / 在座 / 所在 (aspect + lexical).
    (re.compile(r"(?<![正存現所])在(?![於座下])"), "喺"),
    # 了 → 咗 when used as perfective aspect marker. Narrowed: only when the
    # preceding char is a Han char AND the next char is NOT part of a known
    # lexical 了-compound (了解 / 了結 / 了得 / 了然 / 了卻 / 了不起).
    (re.compile(r"(?<=[一-鿿])了(?![解結得然卻不])"), "咗"),
    # 他/她 → 佢 (gendered 3sg collapses in Cantonese).
    (re.compile(r"[他她]"), "佢"),
    # 都 → 都 (same), but 也 → 都.
    (re.compile(r"也(?![許是])"), "都"),
    # 很 → 好.
    (re.compile(r"很(?![多少久])"), "好"),
]


# Sort phrase substitutions by formal-length descending so longer patterns
# (e.g. "不知道") replace before their sub-patterns ("知道").
_PHRASE_SUBS_SORTED: list[tuple[str, str]] = sorted(
    _PHRASE_SUBS, key=lambda kv: len(kv[0]), reverse=True
)


def polish(text: str) -> str:
    """Apply phrase + character substitutions to move formal text toward HK Cantonese.

    Safe to call on any string — if nothing matches, returns `text` unchanged.
    """
    if not text:
        return text
    out = text
    for formal, canto in _PHRASE_SUBS_SORTED:
        if formal == canto:
            continue
        out = out.replace(formal, canto)
    for pat, repl in _CHAR_SUBS:
        out = pat.sub(repl, out)
    return out
