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
import random
import secrets
import sys
import time
from collections.abc import Iterator
from typing import Any, Literal

import httpx

from smcity_fuzz.datasets import TOPICS, DatasetTopic
from smcity_fuzz.judge import JudgeError, judge
from smcity_fuzz.personas import PERSONAS, LanguageCode, Persona
from smcity_fuzz.settings import FuzzSettings, get_fuzz_settings
from smcity_fuzz.store import FuzzRow, append_row, iso_now
from smcity_fuzz.synth import SynthError, synthesise_question
from smcity_fuzz.ws_transport import WsTransportError, drive_turn_via_ws

TransportMode = Literal["http", "ws"]
SamplingMode = Literal["ordered", "shuffled"]


def _new_run_id() -> str:
    return "run-" + secrets.token_hex(6)


def _matrix(
    personas: tuple[Persona, ...],
    topics: tuple[DatasetTopic, ...],
    languages: tuple[LanguageCode, ...],
    *,
    sampling: SamplingMode = "shuffled",
    seed: int | None = None,
) -> Iterator[tuple[Persona, DatasetTopic, LanguageCode]]:
    """Enumerate matrix cells.

    `ordered` — (persona, topic, language) triple-nested loop. Deterministic
    but small `--turns` budgets only see the first persona.

    `shuffled` (default) — same cells, shuffled with a deterministic seed so
    `--turns 40` samples across all personas and languages. Default seed
    derived from `secrets.randbits` so runs vary unless you pin `--seed`.
    """
    cells = [(p, t, lang) for p in personas for t in topics for lang in languages]
    if sampling == "shuffled":
        # Non-cryptographic shuffle — we just want reproducible sampling.
        rng = random.Random(seed if seed is not None else secrets.randbits(32))  # noqa: S311
        rng.shuffle(cells)
    yield from cells


def _progress(
    *,
    idx: int,
    total: int,
    persona: Persona,
    language: LanguageCode,
    topic: DatasetTopic,
    started_at: float,
    row: FuzzRow,
) -> None:
    """Emit one-line progress update to stderr — visible during the run."""
    elapsed_s = time.monotonic() - started_at
    if row.errors:
        status = "FAIL"
        tag = row.errors[0].split(":", 1)[0]
    elif row.judge is None:
        status = "????"
        tag = "no-judge"
    elif row.judge.failed:
        status = "fail"
        tag = ",".join(row.judge.failure_reasons) or f"score={row.judge.total_score}"
    else:
        status = " ok "
        tag = f"score={row.judge.total_score}/10"
    print(
        f"[{idx:>3}/{total}] {status} {language:<8} {persona.id:<18} "
        f"{topic.id:<22} {elapsed_s:>5.1f}s  {tag}",
        file=sys.stderr,
        flush=True,
    )


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
    *,
    mode: TransportMode = "http",
    ws_connect: Any = None,
    idx: int | None = None,
    total: int | None = None,
    emit_progress: bool = True,
) -> FuzzRow:
    async with sem:
        turn_started = time.monotonic()
        row = FuzzRow(
            run_id=run_id,
            ts=iso_now(),
            persona=persona.id,
            language=language,
            topic=topic.id,
            question="",
            transport=mode,
        )

        def _maybe_emit() -> None:
            if emit_progress and idx is not None and total is not None:
                _progress(
                    idx=idx,
                    total=total,
                    persona=persona,
                    language=language,
                    topic=topic,
                    started_at=turn_started,
                    row=row,
                )

        # --- synth ------------------------------------------------------
        try:
            row.question = await synthesise_question(
                persona, topic, language, client=synth_client, settings=s
            )
        except SynthError as err:
            row.errors.append(f"synth:{err}")
            append_row(row, settings=s)
            _maybe_emit()
            return row

        # --- agent call --------------------------------------------------
        # Give each turn a unique session so sessions never accumulate
        # cross-turn state or trigger the rate limiter under concurrency.
        session_id = f"fuzz-{run_id[-8:]}-{secrets.token_hex(4)}"
        reply = ""
        trace: list[dict[str, Any]] = []
        if mode == "ws":
            try:
                ws_result = await drive_turn_via_ws(
                    row.question, session_id, settings=s, connect=ws_connect
                )
                reply = ws_result.reply
                trace = ws_result.tool_trace
                row.reply = reply
                row.tool_trace = trace
                row.elapsed_ms = ws_result.elapsed_ms
                row.ttft_ms = ws_result.ttft_ms
                row.token_count = ws_result.token_count
            except WsTransportError as err:
                row.errors.append(f"agent_ws:{err}")
                append_row(row, settings=s)
                _maybe_emit()
                return row
        else:
            try:
                reply, trace, elapsed = await _call_agent(row.question, session_id, agent_client, s)
                row.reply = reply
                row.tool_trace = trace
                row.elapsed_ms = elapsed
            except httpx.HTTPError as err:
                row.errors.append(f"agent_http:{err}")
                append_row(row, settings=s)
                _maybe_emit()
                return row

        # --- judge -------------------------------------------------------
        try:
            row.judge = await judge(
                row.question, reply, trace, topic, language, client=synth_client, settings=s
            )
        except JudgeError as err:
            row.errors.append(f"judge:{err}")

        append_row(row, settings=s)
        _maybe_emit()
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
    mode: TransportMode = "http",
    ws_connect: Any = None,
    sampling: SamplingMode = "shuffled",
    seed: int | None = None,
    progress: bool = True,
) -> tuple[str, list[FuzzRow]]:
    """Run a fuzz campaign; return (run_id, rows).

    `mode="ws"` drives every turn via the WebSocket `/ws/{session_id}`
    streaming endpoint and captures TTFT + token count on each row.

    `sampling="shuffled"` (default) makes small `--turns` budgets see all
    personas + languages rather than only the first persona. Pass `seed`
    for reproducible orderings.

    `progress=True` (default) emits a one-line status to stderr per turn.
    """
    s = settings or get_fuzz_settings()
    run_id = _new_run_id()
    sem = asyncio.Semaphore(s.concurrency)

    owns_synth = synth_client is None
    owns_agent = agent_client is None
    sc = synth_client or httpx.AsyncClient(timeout=s.timeout_s)
    ac = agent_client or httpx.AsyncClient(timeout=s.agent_timeout_s)

    try:
        cells = list(_matrix(personas, topics, languages, sampling=sampling, seed=seed))
        if max_turns is not None:
            cells = cells[:max_turns]
        total = len(cells)
        if progress:
            print(
                f"fuzz: run_id={run_id} total_turns={total} mode={mode} sampling={sampling}",
                file=sys.stderr,
                flush=True,
            )
        coros = [
            _one_turn(
                p,
                t,
                lang,
                run_id,
                sc,
                ac,
                s,
                sem,
                mode=mode,
                ws_connect=ws_connect,
                idx=i + 1,
                total=total,
                emit_progress=progress,
            )
            for i, (p, t, lang) in enumerate(cells)
        ]
        rows = await asyncio.gather(*coros)
    finally:
        if owns_synth:
            await sc.aclose()
        if owns_agent:
            await ac.aclose()

    return run_id, rows
