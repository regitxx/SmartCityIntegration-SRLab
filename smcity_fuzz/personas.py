"""Hand-authored personas for the adversarial fuzzer.

Each persona is a character the synth LLM role-plays to produce
questions. Personas exist to probe different corners of the agent's
behaviour — language register, politeness, code-switching, hurry,
tech literacy, local vs visitor context.

Personas carry:
- `name_en` / `name_tc` — label (for logs)
- `primary_language` — what they'd default to
- `description_en` — one-line character sheet the synth LLM sees
- `style_hints` — bullet list of phrasing/register notes

We deliberately keep this file small and hand-crafted — it's source of
truth for "what kinds of users does this agent need to serve?".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

LanguageCode = Literal["yue", "zho-Hant", "zho-Hans", "en"]


@dataclass(slots=True, frozen=True)
class Persona:
    id: str
    name_en: str
    primary_language: LanguageCode
    description_en: str
    style_hints: tuple[str, ...]


PERSONAS: tuple[Persona, ...] = (
    Persona(
        id="cantonese_senior",
        name_en="Cantonese senior (Local HK resident, 65+)",
        primary_language="yue",
        description_en=(
            "Lifelong Hong Kong resident, retired, speaks colloquial "
            "Cantonese with particles like 嘅/喺/咗/冇. Uses HK Traditional "
            "Chinese. Doesn't know English tool names or API concepts. "
            "Asks practical everyday questions: buses, pools, free venues."
        ),
        style_hints=(
            "prefer 嘅/喺/咗/冇 over 的/在/了/沒有",
            "use 搵 instead of 找/尋",
            "use 點 not 怎樣; 乜 not 甚麼",
            "sometimes includes 呀/啦/喎/嘞 sentence particles",
            "may refer to places by colloquial nicknames",
        ),
    ),
    Persona(
        id="english_tourist",
        name_en="English-speaking tourist (first visit to HK)",
        primary_language="en",
        description_en=(
            "English-speaking overseas visitor on a short trip. Doesn't "
            "know HK district names well, often misspells station names, "
            "asks about landmarks not addresses. Unfamiliar with Octopus "
            "card, KMB vs Citybus distinction, Cantonese toponyms."
        ),
        style_hints=(
            "casual English, sometimes informal",
            "may misspell HK place names (e.g. 'Shueng Wan', 'Ts Sha Tsui')",
            "asks how-to questions like 'how do I get to…' and 'where's the nearest…'",
            "rarely uses technical jargon",
        ),
    ),
    Persona(
        id="bilingual_student",
        name_en="Bilingual HK university student",
        primary_language="zho-Hant",
        description_en=(
            "Local HK uni student, code-switches between English and "
            "Cantonese or written Traditional Chinese mid-sentence. Uses "
            "HK-specific abbreviations. Fast, casual, comfortable with "
            "apps and shorthand."
        ),
        style_hints=(
            "mix Cantonese / Traditional Chinese / English freely",
            "common shorthand: MTR, KMB, ETA, CWB (Causeway Bay), TST, etc.",
            "may use 粵拼/jyutping or romanisation in passing",
            "short, sometimes terse questions",
        ),
    ),
    Persona(
        id="mainland_visitor",
        name_en="Mainland Chinese visitor (普通话 speaker)",
        primary_language="zho-Hans",
        description_en=(
            "Visitor from mainland China, writes in Simplified Chinese, "
            "uses 普通话 vocabulary (找 instead of 搵, 怎么 instead of 點). "
            "Unfamiliar with HK-specific terms like 港鐵/MTR (may say 地铁), "
            "八達通/Octopus (may say 交通卡), districts."
        ),
        style_hints=(
            "use Simplified Chinese (简体)",
            "use 普通话 vocabulary and grammar",
            "may use 怎么/怎样 instead of 點",
            "may call MTR 地铁 and Octopus 交通卡",
            "polite register with 请/您",
        ),
    ),
    Persona(
        id="rushed_commuter",
        name_en="Rushed HK commuter (mixed language)",
        primary_language="yue",
        description_en=(
            "Working professional in a hurry, uses clipped phrases, often "
            "drops particles, switches between Cantonese and English. "
            "Prioritises speed — asks one-line questions. Real-time "
            "transit is the most common use-case."
        ),
        style_hints=(
            "short, urgent phrasing — often no more than 10 words",
            "may omit verb or subject",
            "mixes English station codes with Cantonese verbs",
            "no polite particles; plain imperative tone",
        ),
    ),
)


def by_id(persona_id: str) -> Persona:
    for p in PERSONAS:
        if p.id == persona_id:
            return p
    raise KeyError(f"unknown persona: {persona_id!r}")
