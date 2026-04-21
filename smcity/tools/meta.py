"""Meta tools — not hitting any upstream, but registered so the LLM can call them.

- `meta.ask_user`: the disambiguation gate. Returns a clarification question the
  orchestrator will surface to the user.
- `meta.what_languages_are_supported`: tells the user which languages each
  upstream dataset actually serves.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from smcity.langrouter.coverage import DATASET_COVERAGE
from smcity.tools.registry import ToolContext, ToolSpec

# --- ask_user --------------------------------------------------------------


class AskUserArgs(BaseModel):
    question: str = Field(
        min_length=1,
        max_length=400,
        description=(
            "Short clarifying question to show the user. Always written in the "
            "user's current locale. Use for slot-filling — especially mode "
            "(MTR / bus / taxi / walking), origin, or destination."
        ),
    )
    slot: str = Field(
        description=(
            "Which slot this question is filling: origin | destination | mode | "
            "venue_type | accessibility | depart_time"
        )
    )


class AskUserResult(BaseModel):
    question: str
    slot: str


async def _ask_user(args: AskUserArgs, ctx: ToolContext) -> AskUserResult:
    return AskUserResult(question=args.question, slot=args.slot)


ASK_USER_TOOL: ToolSpec[AskUserArgs, AskUserResult] = ToolSpec(
    name="meta.ask_user",
    description_en=(
        "Ask the user a short clarifying question before running other tools. "
        "Use this when any of origin / destination / transport mode / venue type "
        "is missing or ambiguous. Prefer one question at a time; do not ask the "
        "same thing again if the user already answered."
    ),
    args_schema=AskUserArgs,
    result_schema=AskUserResult,
    handler=_ask_user,
    ttl_seconds=0,
    budget_ms=50,
    cacheable=False,
    upstream="(none)",
)


# --- what_languages_are_supported -----------------------------------------


class WhatLanguagesArgs(BaseModel):
    tool_name: str | None = Field(
        default=None,
        description="If set, return the language coverage for this specific tool.",
    )


class WhatLanguagesResult(BaseModel):
    coverage: dict[str, list[str]]


async def _what_langs(args: WhatLanguagesArgs, ctx: ToolContext) -> WhatLanguagesResult:
    if args.tool_name:
        langs: list[str] = sorted(DATASET_COVERAGE.get(args.tool_name, set()))
        return WhatLanguagesResult(coverage={args.tool_name: langs})
    return WhatLanguagesResult(
        coverage={name: sorted(langs) for name, langs in DATASET_COVERAGE.items()}
    )


WHAT_LANGUAGES_TOOL: ToolSpec[WhatLanguagesArgs, WhatLanguagesResult] = ToolSpec(
    name="meta.what_languages_are_supported",
    description_en=(
        "Report which languages each underlying dataset natively serves "
        "(EN / 繁體 / 简体). Use when the user explicitly asks 'can you answer in "
        "Korean?' / '日本語 OK?' / similar."
    ),
    args_schema=WhatLanguagesArgs,
    result_schema=WhatLanguagesResult,
    handler=_what_langs,
    ttl_seconds=24 * 60 * 60,
    budget_ms=50,
    upstream="(internal)",
)
