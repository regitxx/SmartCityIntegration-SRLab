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
        f"LANGUAGE you must write in: {_LANG_LABELS[language]}\n\n"
        "RULES:\n"
        "1. Output ONE question only, no preamble, no quotes, no explanation.\n"
        "2. Vary phrasing every time — do NOT use template openings.\n"
        "3. Write as the persona would naturally speak, including typos / "
        "code-switching / shorthand where natural for that persona.\n"
        "4. The question must be answerable with public HK open data — "
        "DO NOT invent private or illegal asks.\n"
        "5. Keep it under 40 words.\n"
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
