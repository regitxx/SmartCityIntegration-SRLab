"""Coverage suite runner.

Reads the question JSONL produced by coverage_gen and POSTs each one to
the agent's /turn endpoint, capturing the full response (reply text +
tool_trace + elapsed + status). Results are appended to a JSONL file
line-by-line so a crashed / killed run can be resumed without re-hitting
the LLM for already-processed questions.

Concurrency is per-question — the agent handles each /turn independently,
and gpt-oss-120b on the Mac Studio comfortably supports 4-8 concurrent
requests in our testing.

Usage::

    python -m smcity_fuzz coverage run \\
        --questions logs/coverage_questions_v1.jsonl \\
        --agent-url https://smcity.taila366aa.ts.net \\
        --concurrency 4 \\
        --out logs/coverage_results_v1.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)


def _read_questions(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            stripped = raw.strip()
            if not stripped:
                continue
            rows.append(json.loads(stripped))
    return rows


def _read_done_ids(path: Path) -> set[str]:
    """Return the set of question IDs already present in the results file —
    skip them on resume."""
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


async def _post_turn(
    client: httpx.AsyncClient,
    *,
    agent_url: str,
    question: dict[str, Any],
    timeout_s: float,
) -> dict[str, Any]:
    """One /turn call. Wraps errors in a structured row so the analyzer
    can categorise failures."""
    session_id = f"cov-{question['id']}"
    lang = question.get("language", "en")
    # Force locale_override so the agent answers in the same language the
    # corpus generated. Detection ambiguity (e.g. yue vs zh-Hant) would
    # otherwise mask Cantonese-path regressions.
    body: dict[str, Any] = {
        "session_id": session_id,
        "text": question["question_en"],
    }
    if lang in {"en", "yue", "zh-Hant", "zh-Hans"}:
        body["locale_override"] = lang
    started = time.perf_counter()
    base_row = {
        "question_id": question["id"],
        "expected_dataset_id": question.get("expected_dataset_id"),
        "expected_tools": question.get("expected_tools") or [],
        "question_en": question["question_en"],
        "question_language": lang,
        "session_id": session_id,
        "ran_at": datetime.now(UTC).isoformat(),
    }
    try:
        r = await client.post(f"{agent_url}/turn", json=body, timeout=timeout_s)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if r.status_code != 200:
            return {
                **base_row,
                "status": "http_error",
                "http_code": r.status_code,
                "reply_text": "",
                "tool_trace": [],
                "elapsed_ms": elapsed_ms,
                "error": r.text[:500],
            }
        d = r.json()
    except httpx.TimeoutException:
        return {
            **base_row,
            "status": "timeout",
            "reply_text": "",
            "tool_trace": [],
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "error": f"timeout after {timeout_s}s",
        }
    except httpx.HTTPError as err:
        return {
            **base_row,
            "status": "network_error",
            "reply_text": "",
            "tool_trace": [],
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "error": str(err)[:500],
        }

    # The agent payload mirrors smcity.schemas.TurnResponse — extract the
    # observable fields we'll analyse.
    #
    # v0.6.3 — we now keep `args`, `result`, and `result_summary` on each
    # trace entry so the LLM judge can grade `factual_vs_trace` against
    # what the tool actually returned, not just the tool name. Previously
    # we discarded these to keep the JSONL small; the judge's hallucination
    # detection was biased downward because it had no ground truth to
    # check claims against.
    return {
        **base_row,
        "status": "ok",
        "reply_text": d.get("text", ""),
        "tool_trace": [
            {
                "name": t.get("name"),
                "args": t.get("args"),
                "status": t.get("status"),
                "latency_ms": t.get("latency_ms"),
                "cached": t.get("cached", False),
                "result_summary": t.get("result_summary"),
                "result": t.get("result"),
            }
            for t in (d.get("tool_trace") or [])
        ],
        "tool_count": len(d.get("tool_trace") or []),
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "agent_elapsed_ms": d.get("elapsed_ms"),
        "lang_detected": (d.get("lang") or {}).get("primary_lang"),
        "citation_count": len(d.get("citations") or []),
        "error": None,
    }


async def run(
    *,
    questions_path: Path,
    agent_url: str,
    output_path: Path,
    concurrency: int = 4,
    timeout_s: float = 120.0,
    limit: int | None = None,
) -> tuple[int, int]:
    """Run the suite. Returns (processed_now, total_in_file)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    all_qs = _read_questions(questions_path)
    if limit is not None:
        all_qs = all_qs[:limit]
    done = _read_done_ids(output_path)
    pending = [q for q in all_qs if q["id"] not in done]
    if not pending:
        print(
            f"[coverage_run] all {len(all_qs)} questions already processed in {output_path}",
            file=sys.stderr,
        )
        return 0, len(all_qs)

    print(
        f"[coverage_run] {len(pending)} pending / {len(all_qs)} total, "
        f"concurrency={concurrency}, agent={agent_url}",
        file=sys.stderr,
    )

    sem = asyncio.Semaphore(concurrency)
    fh = output_path.open("a", encoding="utf-8")
    write_lock = asyncio.Lock()
    processed = 0
    started_at = time.perf_counter()

    async with httpx.AsyncClient() as client:
        async def _worker(q: dict[str, Any]) -> None:
            nonlocal processed
            async with sem:
                row = await _post_turn(
                    client, agent_url=agent_url, question=q, timeout_s=timeout_s
                )
            async with write_lock:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()
                processed += 1
                elapsed = time.perf_counter() - started_at
                rate = processed / max(elapsed, 0.01)
                eta_s = (len(pending) - processed) / max(rate, 0.001)
                print(
                    f"\r[coverage_run] {processed}/{len(pending)} "
                    f"({rate:.2f} q/s, ETA {eta_s/60:.1f} min)            ",
                    end="",
                    file=sys.stderr,
                    flush=True,
                )

        try:
            await asyncio.gather(*(_worker(q) for q in pending))
        finally:
            fh.close()
            print(file=sys.stderr)

    return processed, len(all_qs)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="smcity_fuzz coverage run")
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument(
        "--agent-url",
        default="https://smcity.taila366aa.ts.net",
        help="Base URL of the running agent.",
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout-s", type=float, default=120.0)
    parser.add_argument(
        "--limit", type=int, default=None, help="Cap the number of questions (smoke testing)."
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    processed, total = asyncio.run(
        run(
            questions_path=args.questions,
            agent_url=args.agent_url,
            output_path=args.out,
            concurrency=args.concurrency,
            timeout_s=args.timeout_s,
            limit=args.limit,
        )
    )
    print(f"processed {processed} new questions ({total} total in source)", file=sys.stderr)
    return 0


__all__ = ["main", "run"]


if __name__ == "__main__":
    sys.exit(main())
