# ruff: noqa: RUF001, RUF003
"""Adversarial question synthesiser.

Given a (persona, topic, language) tuple, asks the fuzzer LLM to produce
ONE question a real Hong Kong user with that persona would ask about
that topic. The LLM is deliberately primed to vary phrasing each call
(typos welcome, code-switching welcome, short AND long forms).

The call is a plain OpenAI-compatible `/chat/completions` — we use httpx
directly rather than the openai SDK to keep mocking simple in tests.
"""

from __future__ import annotations

import httpx

from smcity_fuzz.datasets import DatasetTopic
from smcity_fuzz.personas import LanguageCode, Persona
from smcity_fuzz.settings import FuzzSettings, get_fuzz_settings

_LANG_LABELS: dict[LanguageCode, str] = {
    "yue": "Cantonese (HK, colloquial, Traditional characters with particles like 嘅/喺/咗)",
    "zho-Hant": "Traditional Chinese (HK / Taiwan register)",
    "zho-Hans": "Simplified Chinese (Mainland register)",
    "en": "English",
}

# Strict one-line constraint appended after everything else. Previous
# v0.4.x synth let the 20b drift — e.g. asking a "Cantonese senior" to
# write in English produced mostly Cantonese output, which then got
# labelled as "en" in the JSONL and the judge (correctly) flagged
# wrong_language. Fixing by making the language constraint the LAST
# and LOUDEST rule in the prompt.
_STRICT_LANG_RULES: dict[LanguageCode, str] = {
    "yue": (
        "THE QUESTION MUST BE IN CANTONESE (yue). Traditional Chinese characters "
        "with at least one colloquial particle from the set 嘅/喺/咗/冇/佢/唔/係/呀/啦. "
        "No Mandarin 的/在/了/沒/是. No English sentences (single-word brand / station "
        "names like MTR or 'Central' are fine)."
    ),
    "zho-Hant": (
        "THE QUESTION MUST BE IN TRADITIONAL CHINESE, Mandarin register "
        "(the/在/了/沒/是). Do NOT use Cantonese particles like 嘅/喺/咗/冇/唔. "
        "Traditional characters only — do NOT use Simplified."
    ),
    "zho-Hans": (
        "THE QUESTION MUST BE IN SIMPLIFIED CHINESE, Mainland Mandarin register. "
        "Do NOT use Cantonese particles (嘅/喺/咗/冇/唔) even as code-switching. "
        "Simplified characters only (e.g. 现在 not 現在)."
    ),
    "en": (
        "THE QUESTION MUST BE IN ENGLISH. No Chinese characters except at most "
        "ONE HK place name if natural (e.g. 'Tsim Sha Tsui', 'Mong Kok'). "
        "Do NOT write any Cantonese/Mandarin sentence fragments."
    ),
}


def _system_prompt(persona: Persona, topic: DatasetTopic, language: LanguageCode) -> str:
    hints = "\n".join(f"  - {h}" for h in persona.style_hints)
    return (
        "You are role-playing a real user of a Hong Kong smart-city "
        "assistant. Your job is to CRAFT ONE NATURAL QUESTION this user "
        "would actually ask.\n\n"
        f"USER PERSONA: {persona.name_en}\n"
        f"{persona.description_en}\n"
        f"Phrasing notes:\n{hints}\n\n"
        f"TOPIC the user is curious about: {topic.title_en} ({topic.title_tc})\n"
        f"Topic description: {topic.description_en}\n\n"
        f"LANGUAGE: {_LANG_LABELS[language]}\n"
        f"  LANGUAGE RULE: {_STRICT_LANG_RULES[language]}\n"
        "  (The persona description may imply a different native language — "
        "   IGNORE it. The LANGUAGE RULE above wins absolutely.)\n\n"
        "OUTPUT RULES:\n"
        "1. Output ONE question only, no preamble, no quotes, no explanation.\n"
        "2. Vary phrasing every time — do NOT use template openings.\n"
        "3. The question must be answerable with public HK open data — "
        "DO NOT invent private or illegal asks.\n"
        "4. Keep it under 40 words.\n"
        "5. End with exactly one question mark (? or ？).\n"
    )


class SynthError(RuntimeError):
    """Raised when the fuzzer LLM can't produce a question."""


async def synthesise_question(
    persona: Persona,
    topic: DatasetTopic,
    language: LanguageCode,
    *,
    client: httpx.AsyncClient | None = None,
    settings: FuzzSettings | None = None,
    temperature: float = 1.0,
) -> str:
    """Call the fuzzer LLM to produce one question.

    Raises `SynthError` on any upstream / parse failure so the runner
    can record it and move on without crashing the campaign.
    """
    s = settings or get_fuzz_settings()
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=s.timeout_s)
    try:
        try:
            resp = await http.post(
                f"{s.base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": "Bearer lm-studio"},
                json={
                    "model": s.model,
                    "temperature": temperature,
                    "max_tokens": 200,
                    "messages": [
                        {"role": "system", "content": _system_prompt(persona, topic, language)},
                        {"role": "user", "content": "Generate the question now."},
                    ],
                },
            )
            resp.raise_for_status()
            payload = resp.json()
        except httpx.HTTPError as err:
            raise SynthError(f"synth HTTP failed: {err}") from err
        except ValueError as err:
            raise SynthError(f"synth non-JSON: {err}") from err

        try:
            text = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as err:
            raise SynthError(f"synth malformed payload: {err}") from err

        question = (text or "").strip()
        # Strip optional "Question:" / "Q:" / 問：/ 问： prefix, then outer
        # quotes. Some models emit `Question: "..."` — we want neither.
        for prefix in ("Question:", "Q:", "問：", "问："):
            if question.startswith(prefix):
                question = question[len(prefix) :].strip()
                break
        question = question.strip('"').strip("'").strip()
        if not question:
            raise SynthError("synth returned empty content")
        return question
    finally:
        if owns_client:
            await http.aclose()
