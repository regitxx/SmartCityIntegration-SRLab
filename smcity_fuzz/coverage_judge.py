"""Run the LLM-as-judge (smcity_fuzz.judge.judge) over a coverage_run output.

Reads a JSONL produced by ``coverage_run``, calls the judge for every row
that hasn't been judged yet, writes structured verdicts to a JSONL. Same
resume pattern as ``coverage_run``: a row whose ``question_id`` already
appears in the output is skipped on a re-run, so a killed process picks
up where it left off.

The judge expects a ``DatasetTopic`` and a ``LanguageCode``. Coverage rows
carry the workbook S-id (``expected_dataset_id``) and a language tag
(``zh-Hant`` / ``zh-Hans`` / ``yue`` / ``en``) — neither matches the
existing ``TOPICS`` table exactly, so we synthesise a minimal topic from
the row's own metadata and remap the language code.

Usage::

    python -m smcity_fuzz.coverage_judge \\
        --results logs/coverage_results_v0.6.2_sample500.jsonl \\
        --out logs/coverage_judged_v0.6.2_sample500.jsonl \\
        --concurrency 2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from smcity_fuzz.datasets import DatasetTopic
from smcity_fuzz.judge import JudgeError, judge
from smcity_fuzz.personas import LanguageCode
from smcity_fuzz.settings import get_fuzz_settings


# Map the coverage-row language tag onto judge.py's LanguageCode literal.
_LANG_REMAP: dict[str, LanguageCode] = {
    "yue": "yue",
    "en": "en",
    "zh-Hant": "zho-Hant",
    "zh-Hans": "zho-Hans",
}


def _topic_from_row(row: dict[str, Any]) -> DatasetTopic:
    """Build a minimal DatasetTopic from the coverage-run row.

    The judge uses ``title_en``, ``description_en``, and ``expected_tools``
    in its system prompt; the other fields are tolerated as empty. We pull
    these from the row so the judge sees the same dataset framing the
    grader contracts used.
    """
    return DatasetTopic(
        id=str(row.get("expected_dataset_id") or "unknown"),
        title_en=str(row.get("expected_dataset_title") or row.get("expected_dataset_id") or "unknown"),
        title_tc="",
        expected_tools=tuple(row.get("expected_tools") or ()),
        description_en=(
            f"Dataset {row.get('expected_dataset_id')} "
            f"({row.get('expected_dataset_category') or 'unknown category'}). "
            "Agent should have called the expected_tools and produced a reply "
            "that uses real data, in the user's language."
        ),
    )


def _read_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            stripped = raw.strip()
            if not stripped:
                continue
            rows.append(json.loads(stripped))
    return rows


def _read_done_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    done: set[str] = set()
    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            qid = row.get("question_id")
            if qid:
                done.add(qid)
    return done


async def _judge_one(
    client: httpx.AsyncClient,
    row: dict[str, Any],
) -> dict[str, Any]:
    qid = row.get("question_id")
    lang_tag = row.get("question_language") or "en"
    lang: LanguageCode = _LANG_REMAP.get(lang_tag, "en")
    topic = _topic_from_row(row)

    base = {
        "question_id": qid,
        "expected_dataset_id": row.get("expected_dataset_id"),
        "question_language": lang_tag,
        "question": row.get("question_en"),
        "reply": row.get("reply_text"),
        "judged_at": datetime.now(UTC).isoformat(),
    }

    # If the row was not OK we still ask the judge — the agent's fallback
    # text may itself be a reasonable failure mode (e.g. polite refusal),
    # OR it may be the orchestrator's fallback "I couldn't compose an
    # answer" which deserves a 0 on intent_match.
    started = time.perf_counter()
    try:
        verdict = await judge(
            question=str(row.get("question_en") or ""),
            reply=str(row.get("reply_text") or ""),
            tool_trace=list(row.get("tool_trace") or []),
            topic=topic,
            language=lang,
            client=client,
        )
    except JudgeError as err:
        return {
            **base,
            "status": "judge_error",
            "error": str(err)[:500],
            "judge_latency_ms": int((time.perf_counter() - started) * 1000),
        }
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return {
        **base,
        "status": "ok",
        "intent_match": verdict.intent_match,
        "language_ok": verdict.language_ok,
        "tool_choice_ok": verdict.tool_choice_ok,
        "factual_vs_trace": verdict.factual_vs_trace,
        "coherence": verdict.coherence,
        "total_score": verdict.total_score,
        "failed": verdict.failed,
        "failure_reasons": verdict.failure_reasons,
        "summary": verdict.summary,
        "judge_latency_ms": elapsed_ms,
    }


async def run(
    *,
    results_path: Path,
    output_path: Path,
    concurrency: int = 2,
    timeout_s: float = 60.0,
    limit: int | None = None,
) -> tuple[int, int]:
    """Drive the judge over `results_path`. Returns (judged_now, total_in_file)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    s = get_fuzz_settings()
    all_rows = _read_rows(results_path)
    if limit is not None:
        all_rows = all_rows[:limit]
    done = _read_done_ids(output_path)
    pending = [r for r in all_rows if r.get("question_id") and r["question_id"] not in done]
    if not pending:
        print(
            f"[coverage_judge] all {len(all_rows)} already judged in {output_path}",
            file=sys.stderr,
        )
        return 0, len(all_rows)

    print(
        f"[coverage_judge] {len(pending)} pending / {len(all_rows)} total, "
        f"concurrency={concurrency}, model={s.model}, base={s.base_url}",
        file=sys.stderr,
    )

    sem = asyncio.Semaphore(concurrency)
    fh = output_path.open("a", encoding="utf-8")
    write_lock = asyncio.Lock()
    started_at = time.perf_counter()
    processed = 0

    async with httpx.AsyncClient(timeout=timeout_s) as client:

        async def _worker(r: dict[str, Any]) -> None:
            nonlocal processed
            async with sem:
                verdict_row = await _judge_one(client, r)
            async with write_lock:
                fh.write(json.dumps(verdict_row, ensure_ascii=False) + "\n")
                fh.flush()
                processed += 1
                elapsed = time.perf_counter() - started_at
                rate = processed / max(elapsed, 0.01)
                eta_s = (len(pending) - processed) / max(rate, 0.001)
                print(
                    f"\r[coverage_judge] {processed}/{len(pending)} "
                    f"({rate:.2f} q/s, ETA {eta_s/60:.1f} min)            ",
                    end="",
                    file=sys.stderr,
                    flush=True,
                )

        try:
            await asyncio.gather(*(_worker(r) for r in pending))
        finally:
            fh.close()
            print(file=sys.stderr)

    return processed, len(all_rows)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="smcity_fuzz coverage judge")
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--timeout-s", type=float, default=60.0)
    parser.add_argument(
        "--limit", type=int, default=None, help="Cap the number of rows judged."
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    judged, total = asyncio.run(
        run(
            results_path=args.results,
            output_path=args.out,
            concurrency=args.concurrency,
            timeout_s=args.timeout_s,
            limit=args.limit,
        )
    )
    print(f"judged {judged} new rows ({total} total)", file=sys.stderr)
    return 0


__all__ = ["main", "run"]


if __name__ == "__main__":
    sys.exit(main())
