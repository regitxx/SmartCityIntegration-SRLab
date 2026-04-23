"""CLI for smcity_fuzz.

Usage:
    uv run python -m smcity_fuzz run [--turns N] [--concurrency K]
                                     [--personas id,id] [--languages yue,en]
                                     [--topics id,id]
    uv run python -m smcity_fuzz report [--run-id run-xxx]
    uv run python -m smcity_fuzz failures [--run-id run-xxx] [--top N]
    uv run python -m smcity_fuzz export [--run-id run-xxx] [--out PATH]
                                        [--max-failures N] [--all]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from smcity_fuzz.datasets import TOPICS, DatasetTopic
from smcity_fuzz.datasets import by_id as topic_by_id
from smcity_fuzz.export import render_report
from smcity_fuzz.personas import PERSONAS, LanguageCode, Persona
from smcity_fuzz.personas import by_id as persona_by_id
from smcity_fuzz.report import summarise
from smcity_fuzz.runner import run_campaign
from smcity_fuzz.settings import get_fuzz_settings
from smcity_fuzz.store import FuzzRow, read_rows

_DEFAULT_LANGS: tuple[LanguageCode, ...] = ("yue", "en", "zho-Hant", "zho-Hans")


def _parse_personas(raw: str | None) -> tuple[Persona, ...]:
    if not raw:
        return PERSONAS
    return tuple(persona_by_id(tok.strip()) for tok in raw.split(",") if tok.strip())


def _parse_topics(raw: str | None) -> tuple[DatasetTopic, ...]:
    if not raw:
        return TOPICS
    return tuple(topic_by_id(tok.strip()) for tok in raw.split(",") if tok.strip())


def _parse_langs(raw: str | None) -> tuple[LanguageCode, ...]:
    if not raw:
        return _DEFAULT_LANGS
    allowed: set[str] = {"yue", "zho-Hant", "zho-Hans", "en"}
    out: list[LanguageCode] = []
    for tok in raw.split(","):
        t = tok.strip()
        if not t:
            continue
        if t not in allowed:
            raise SystemExit(f"unknown language {t!r}; expected one of {sorted(allowed)}")
        out.append(t)  # type: ignore[arg-type]
    return tuple(out)


async def _cmd_run(args: argparse.Namespace) -> int:
    personas = _parse_personas(args.personas)
    topics = _parse_topics(args.topics)
    languages = _parse_langs(args.languages)

    settings = get_fuzz_settings()
    if args.concurrency:
        settings = settings.model_copy(update={"concurrency": args.concurrency})

    run_id, rows = await run_campaign(
        personas=personas,
        topics=topics,
        languages=languages,
        max_turns=args.turns,
        settings=settings,
    )
    print(f"run_id: {run_id}")
    print(summarise(rows, max_failures_shown=args.show))
    return 0


def _filter_rows(rows: Sequence[FuzzRow], run_id: str | None) -> list[FuzzRow]:
    if run_id is None:
        return list(rows)
    return [r for r in rows if r.run_id == run_id]


def _cmd_report(args: argparse.Namespace) -> int:
    rows = _filter_rows(read_rows(), args.run_id)
    if not rows:
        print("no rows found")
        return 1
    print(summarise(rows, max_failures_shown=args.show))
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    rows = _filter_rows(read_rows(), args.run_id)
    if not rows:
        print("no rows found")
        return 1
    markdown = render_report(
        rows,
        only_failures=not args.all,
        max_failures=args.max_failures,
    )
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(markdown, encoding="utf-8")
        print(f"wrote {out_path} ({len(markdown):,} chars)")
    else:
        print(markdown)
    return 0


def _cmd_failures(args: argparse.Namespace) -> int:
    rows = _filter_rows(read_rows(), args.run_id)
    failed = [r for r in rows if r.failed]
    if not failed:
        print("no failures found")
        return 0
    failed = failed[: args.top]
    for i, r in enumerate(failed, 1):
        reasons: list[str] = list(r.errors)
        if r.judge:
            reasons.extend(r.judge.failure_reasons)
        reasons_str = ", ".join(reasons) or "(no tags)"
        print(f"[{i:>2}] {r.ts}  {r.persona}/{r.language}/{r.topic}  ⇒  {reasons_str}")
        print(f"     Q: {r.question}")
        print(f"     R: {(r.reply or '').replace(chr(10), ' ')[:300]}")
        if r.judge:
            print(f"     J: {r.judge.summary}")
        print()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="smcity_fuzz")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Run a new fuzz campaign")
    p_run.add_argument(
        "--turns",
        type=int,
        default=None,
        help="cap total turns (default: full matrix)",
    )
    p_run.add_argument("--concurrency", type=int, default=None)
    p_run.add_argument("--personas", type=str, default=None, help="comma-separated persona IDs")
    p_run.add_argument("--topics", type=str, default=None, help="comma-separated topic IDs")
    p_run.add_argument(
        "--languages",
        type=str,
        default=None,
        help="comma-separated language codes (yue, en, zho-Hant, zho-Hans)",
    )
    p_run.add_argument("--show", type=int, default=15, help="top-N failures to print")

    p_report = sub.add_parser("report", help="Summarise a past campaign")
    p_report.add_argument("--run-id", type=str, default=None)
    p_report.add_argument("--show", type=int, default=15)

    p_fail = sub.add_parser("failures", help="Print failing rows in detail")
    p_fail.add_argument("--run-id", type=str, default=None)
    p_fail.add_argument("--top", type=int, default=20)

    p_exp = sub.add_parser(
        "export",
        help=(
            "Render a Markdown report for handoff to Claude / Gemini. Each "
            "failure includes the question, reply, tool_trace, judge verdict "
            "and the raw row JSON."
        ),
    )
    p_exp.add_argument("--run-id", type=str, default=None)
    p_exp.add_argument(
        "--out",
        type=str,
        default=None,
        help="write to FILE; omit to stream to stdout",
    )
    p_exp.add_argument(
        "--max-failures",
        type=int,
        default=None,
        help="cap the number of failure sections included (default: all)",
    )
    p_exp.add_argument(
        "--all",
        action="store_true",
        help="include passing rows too (default: failures only)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "run":
        return asyncio.run(_cmd_run(args))
    if args.cmd == "report":
        return _cmd_report(args)
    if args.cmd == "failures":
        return _cmd_failures(args)
    if args.cmd == "export":
        return _cmd_export(args)
    parser.error(f"unknown command {args.cmd!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
