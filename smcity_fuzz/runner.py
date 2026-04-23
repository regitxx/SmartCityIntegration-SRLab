# ruff: noqa: RUF002
"""Fuzz runner — coordinates synth → agent call → judge → store.

The `run_campaign` coroutine is the one CLI entrypoint uses. It:

1. Builds the (persona × topic × language) matrix.
2. For each cell, spawns a bounded-concurrency task that:
   a. calls synth → question
   b. POSTs /turn on the agent → (reply, tool_trace, elapsed_ms)
   c. calls judge → verdict
   d. writes a row to JSONL
3. Returns the `run_id` + counts.

Failures at any stage are recorded in the row's `errors[]` so the
campaign never aborts on a single bad turn.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Iterator
from typing import Any

import httpx

from smcity_fuzz.datasets import TOPICS, DatasetTopic
from smcity_fuzz.judge import JudgeError, judge
from smcity_fuzz.personas import PERSONAS, LanguageCode, Persona
from smcity_fuzz.settings import FuzzSettings, get_fuzz_settings
from smcity_fuzz.store import FuzzRow, append_row, iso_now
from smcity_fuzz.synth import SynthError, synthesise_question


def _new_run_id() -> str:
    return "run-" + secrets.token_hex(6)


def _matrix(
    personas: tuple[Persona, ...],
    topics: tuple[DatasetTopic, ...],
    languages: tuple[LanguageCode, ...],
) -> Iterator[tuple[Persona, DatasetTopic, LanguageCode]]:
    for p in personas:
        for t in topics:
            for lang in languages:
                yield p, t, lang


async def _call_agent(
    question: str,
    session_id: str,
    agent_client: httpx.AsyncClient,
    s: FuzzSettings,
) -> tuple[str, list[dict[str, Any]], int]:
    """POST /turn and normalise the response to (reply, tool_trace, elapsed_ms)."""
    resp = await agent_client.post(
        f"{s.agent_url.rstrip('/')}/turn",
        json={"session_id": session_id, "text": question},
        timeout=s.agent_timeout_s,
    )
    resp.raise_for_status()
    data = resp.json()
    reply = data.get("text") or ""
    trace = data.get("tool_trace") or []
    elapsed = int(data.get("elapsed_ms") or 0)
    return reply, trace, elapsed


async def _one_turn(
    persona: Persona,
    topic: DatasetTopic,
    language: LanguageCode,
    run_id: str,
    synth_client: httpx.AsyncClient,
    agent_client: httpx.AsyncClient,
    s: FuzzSettings,
    sem: asyncio.Semaphore,
) -> FuzzRow:
    async with sem:
        row = FuzzRow(
            run_id=run_id,
            ts=iso_now(),
            persona=persona.id,
            language=language,
            topic=topic.id,
            question="",
        )

        # --- synth ------------------------------------------------------
        try:
            row.question = await synthesise_question(
                persona, topic, language, client=synth_client, settings=s
            )
        except SynthError as err:
            row.errors.append(f"synth:{err}")
            append_row(row, settings=s)
            return row

        # --- agent call --------------------------------------------------
        # Give each turn a unique session so sessions never accumulate
        # cross-turn state or trigger the rate limiter under concurrency.
        session_id = f"fuzz-{run_id[-8:]}-{secrets.token_hex(4)}"
        try:
            reply, trace, elapsed = await _call_agent(row.question, session_id, agent_client, s)
            row.reply = reply
            row.tool_trace = trace
            row.elapsed_ms = elapsed
        except httpx.HTTPError as err:
            row.errors.append(f"agent_http:{err}")
            append_row(row, settings=s)
            return row

        # --- judge -------------------------------------------------------
        try:
            row.judge = await judge(
                row.question, reply, trace, topic, language, client=synth_client, settings=s
            )
        except JudgeError as err:
            row.errors.append(f"judge:{err}")

        append_row(row, settings=s)
        return row


async def run_campaign(
    *,
    personas: tuple[Persona, ...] = PERSONAS,
    topics: tuple[DatasetTopic, ...] = TOPICS,
    languages: tuple[LanguageCode, ...] = ("yue", "en", "zho-Hant", "zho-Hans"),
    max_turns: int | None = None,
    settings: FuzzSettings | None = None,
    synth_client: httpx.AsyncClient | None = None,
    agent_client: httpx.AsyncClient | None = None,
) -> tuple[str, list[FuzzRow]]:
    """Run a fuzz campaign; return (run_id, rows)."""
    s = settings or get_fuzz_settings()
    run_id = _new_run_id()
    sem = asyncio.Semaphore(s.concurrency)

    owns_synth = synth_client is None
    owns_agent = agent_client is None
    sc = synth_client or httpx.AsyncClient(timeout=s.timeout_s)
    ac = agent_client or httpx.AsyncClient(timeout=s.agent_timeout_s)

    try:
        cells = list(_matrix(personas, topics, languages))
        if max_turns is not None:
            cells = cells[:max_turns]
        coros = [_one_turn(p, t, lang, run_id, sc, ac, s, sem) for p, t, lang in cells]
        rows = await asyncio.gather(*coros)
    finally:
        if owns_synth:
            await sc.aclose()
        if owns_agent:
            await ac.aclose()

    return run_id, rows
