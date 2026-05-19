"""Meta tools — not hitting any upstream, but registered so the LLM can call them.

- `meta.ask_user`: the disambiguation gate.
- `meta.what_languages_are_supported`: per-tool language coverage.
- `meta.forget_me`: wipes the session record + clears conversation history.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from smcity.langrouter.coverage import DATASET_COVERAGE
from smcity.session import SessionStore
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


# --- forget_me -----------------------------------------------------------

_DEFAULT_DB = Path(__file__).resolve().parent.parent.parent / "state" / "sessions.sqlite3"


class ForgetMeArgs(BaseModel):
    pass


class ForgetMeResult(BaseModel):
    ok: bool
    session_id: str


async def _forget_me(args: ForgetMeArgs, ctx: ToolContext) -> ForgetMeResult:
    store = SessionStore(_DEFAULT_DB)
    await store.forget(ctx.session_id)
    return ForgetMeResult(ok=True, session_id=ctx.session_id)


FORGET_ME_TOOL: ToolSpec[ForgetMeArgs, ForgetMeResult] = ToolSpec(
    name="meta.forget_me",
    description_en=(
        "Wipe the current session's stored state (conversation history, slots, "
        "locale). Call when the user explicitly asks to forget / reset / start "
        "over / delete data. The user will need to re-introduce their origin / "
        "destination on the next turn."
    ),
    args_schema=ForgetMeArgs,
    result_schema=ForgetMeResult,
    handler=_forget_me,
    ttl_seconds=0,
    budget_ms=200,
    cacheable=False,
    upstream="(internal)",
)
