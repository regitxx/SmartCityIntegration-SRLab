"""LLM-as-judge — rubric scorer for (question, reply, tool_trace) triples.

The judge sees what tools the agent called (`tool_trace`), so it can
catch factual drift between the tool data and the final reply — not
just surface-level language match.

Rubric (each field is 0 = fail, 1 = partial, 2 = excellent):

- intent_match       : did the agent understand what the user actually asked?
- language_ok        : did it reply in the same language as the question?
- tool_choice_ok     : was the chosen tool appropriate? (uses expected_tools hint)
- factual_vs_trace   : do the claims in the reply match what the tools returned?
- coherence          : natural, concise, usable reply?

Plus `failure_reasons: string[]` with short machine-readable tags, and
`summary: string` with one sentence of judge reasoning.

Thresholds (defaults, overridable in `report.py`):
- A row is `failed` if ANY rubric score < 1, OR if `intent_match == 0`,
  OR if `failure_reasons` is non-empty.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError

from smcity_fuzz.datasets import DatasetTopic
from smcity_fuzz.personas import LanguageCode
from smcity_fuzz.settings import FuzzSettings, get_fuzz_settings

_LANG_LABELS: dict[LanguageCode, str] = {
    "yue": "Cantonese (HK)",
    "zho-Hant": "Traditional Chinese",
    "zho-Hans": "Simplified Chinese",
    "en": "English",
}


class JudgeVerdict(BaseModel):
    intent_match: int = Field(ge=0, le=2)
    language_ok: int = Field(ge=0, le=2)
    tool_choice_ok: int = Field(ge=0, le=2)
    factual_vs_trace: int = Field(ge=0, le=2)
    coherence: int = Field(ge=0, le=2)
    failure_reasons: list[str] = Field(default_factory=list)
    summary: str = ""

    @property
    def failed(self) -> bool:
        scores = [
            self.intent_match,
            self.language_ok,
            self.tool_choice_ok,
            self.factual_vs_trace,
            self.coherence,
        ]
        return any(s < 1 for s in scores) or bool(self.failure_reasons)

    @property
    def total_score(self) -> int:
        return (
            self.intent_match
            + self.language_ok
            + self.tool_choice_ok
            + self.factual_vs_trace
            + self.coherence
        )


class JudgeError(RuntimeError):
    """Raised when the judge LLM can't produce a parseable verdict."""


def _system_prompt(topic: DatasetTopic, language: LanguageCode) -> str:
    tool_hints = ", ".join(topic.expected_tools) or "(no preferred tool)"
    return (
        "You are a strict grader for a Hong Kong smart-city assistant. "
        "You receive a user question, the agent's reply, and the "
        "tool_trace showing which tools the agent called. Score the "
        "agent's answer against a 5-dimension rubric and return STRICT "
        "JSON.\n\n"
        "YOUR ROLE IS DIAGNOSTIC ONLY.\n"
        " - You MUST NOT suggest, write, or describe any code fix.\n"
        " - You MUST NOT recommend prompt changes or new tools.\n"
        " - You MUST NOT speculate about why the bug exists inside the "
        "agent's implementation.\n"
        " - Your job is to describe what went wrong in the *output*, "
        "nothing more. A separate engineer (or a different LLM like "
        "Claude / Gemini) will receive your report and decide on fixes.\n\n"
        f"Question language: {_LANG_LABELS[language]}\n"
        f"Topic: {topic.title_en} — {topic.description_en}\n"
        f"Expected tools (hint — agent may legitimately use others): {tool_hints}\n\n"
        "Rubric (each 0 / 1 / 2):\n"
        "  intent_match     — did the agent understand what was asked?\n"
        "  language_ok      — reply in same language as the question?\n"
        "  tool_choice_ok   — reasonable tool(s) given the topic?\n"
        "  factual_vs_trace — claims consistent with tool_trace outputs?\n"
        "  coherence        — natural, clear, usable reply?\n\n"
        "Also list short failure_reasons tags from this controlled set:\n"
        "  wrong_language, hallucinated_fact, tool_error, empty_reply, "
        "harmony_leak, wrong_tool, refused_wrongly, english_in_cantonese, "
        "mandarin_in_cantonese, stale_data, incomplete\n\n"
        "`summary` must be ONE sentence describing the observable defect "
        "(e.g. 'Replied in English despite Cantonese question', "
        "'Cited Pool A but tool returned Pool B'). Do not write fixes.\n\n"
        "Output EXACTLY ONE JSON object with keys:\n"
        '  {"intent_match":int, "language_ok":int, "tool_choice_ok":int, '
        '"factual_vs_trace":int, "coherence":int, '
        '"failure_reasons":[string,...], "summary":"one sentence"}\n\n'
        "Do NOT wrap in markdown fences, do NOT add commentary, JSON only."
    )


_RESULT_PREVIEW_CHARS = 1500


def _format_result_for_judge(result: object | None) -> str:
    """Serialise the raw tool result for the judge, truncated if huge.

    The point of including the full result (not just `result_summary`) is
    so the judge can actually verify numeric claims like "next train in 3
    minutes" against what the tool returned. Over ~1.5 KB of JSON per
    tool gets diminishing returns and blows the model's context, so we
    truncate and note it.
    """
    if result is None:
        return "(none)"
    try:
        blob = json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError):
        return "(unserialisable)"
    if len(blob) <= _RESULT_PREVIEW_CHARS:
        return blob
    return blob[:_RESULT_PREVIEW_CHARS] + f"…(+{len(blob) - _RESULT_PREVIEW_CHARS} chars)"


def _user_prompt(question: str, reply: str, tool_trace: list[dict[str, Any]]) -> str:
    trace_lines = []
    for t in tool_trace:
        trace_lines.append(
            f"  - {t.get('name')}: status={t.get('status')} "
            f"args={json.dumps(t.get('args'), ensure_ascii=False)}\n"
            f"      summary: {t.get('result_summary') or '(none)'}\n"
            f"      raw_result: {_format_result_for_judge(t.get('result'))}"
        )
    trace_str = "\n".join(trace_lines) if trace_lines else "  (no tools called)"
    return (
        "QUESTION:\n" + question + "\n\n"
        "AGENT REPLY:\n" + (reply or "(empty)") + "\n\n"
        "TOOL TRACE (raw_result is the truth — summary is just a shorthand label):\n"
        + trace_str
        + "\n\n"
        "Return the JSON verdict now."
    )


def _strip_code_fence(text: str) -> str:
    """Best-effort extraction of the first JSON object from model output."""
    text = text.strip()
    # Strip ```json ... ``` fences if present.
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    # Trim to first {...} pair.
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last != -1 and last > first:
        text = text[first : last + 1]
    return text


async def judge(
    question: str,
    reply: str,
    tool_trace: list[dict[str, Any]],
    topic: DatasetTopic,
    language: LanguageCode,
    *,
    client: httpx.AsyncClient | None = None,
    settings: FuzzSettings | None = None,
    temperature: float = 0.0,
) -> JudgeVerdict:
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
                    "max_tokens": 400,
                    "messages": [
                        {"role": "system", "content": _system_prompt(topic, language)},
                        {"role": "user", "content": _user_prompt(question, reply, tool_trace)},
                    ],
                },
            )
            resp.raise_for_status()
            payload = resp.json()
        except httpx.HTTPError as err:
            raise JudgeError(f"judge HTTP failed: {err}") from err
        except ValueError as err:
            raise JudgeError(f"judge non-JSON: {err}") from err

        try:
            raw = payload["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as err:
            raise JudgeError(f"judge malformed payload: {err}") from err

        cleaned = _strip_code_fence(raw)
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as err:
            raise JudgeError(
                f"judge returned non-parseable JSON: {err}; raw={raw[:200]!r}"
            ) from err

        try:
            return JudgeVerdict.model_validate(data)
        except ValidationError as err:
            raise JudgeError(f"judge JSON failed schema: {err}") from err
    finally:
        if owns_client:
            await http.aclose()
