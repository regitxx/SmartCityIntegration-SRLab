"""Render a fuzz run as a single Markdown file for handoff.

The canonical consumer is a frontier LLM (Claude / Gemini) that receives
this file as part of a chat message and is asked to diagnose + propose
fixes. The fuzzer's own LLM (gpt-oss-20b) NEVER proposes fixes — it only
classifies defects. This export preserves that separation: the file is
shaped as raw evidence, not as a fix proposal.

Format: Markdown with a strong banner, a summary table, and one
`## Failure N` section per failing row containing question, reply,
tool_trace, judge verdict, and full JSON payload fenced for copying.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence

from smcity_fuzz.store import FuzzRow

_BANNER = """\
# smcity fuzz report — diagnostic handoff

**This file is a diagnostic report produced by an adversarial LLM fuzzer.**

- Each "Failure" below is a real turn against the production agent where the
  gpt-oss-20b judge flagged a defect.
- The judge is configured to describe defects only. It does NOT propose code
  fixes — that is intentional. A separate engineer (or a frontier LLM like
  Claude / Gemini receiving this file) is expected to decide fixes.
- `tool_trace` shows which tools the agent called. Use it to distinguish
  hallucinated facts (agent claim not supported by any tool) from
  tool-choice errors (wrong tool was called) from upstream failures
  (tool returned an error status).
- When asking an LLM to review: paste this whole file, then say
  "diagnose the top 5 failures and propose minimal code patches."
"""


def _summary_counts(rows: Sequence[FuzzRow]) -> str:
    total = len(rows)
    failed = [r for r in rows if r.failed]
    passed = total - len(failed)

    by_reason: Counter[str] = Counter()
    by_topic: Counter[str] = Counter()
    by_lang: Counter[str] = Counter()
    for r in failed:
        if r.judge:
            for tag in r.judge.failure_reasons:
                by_reason[tag] += 1
        for err in r.errors:
            by_reason[err.split(":", 1)[0]] += 1
        by_topic[r.topic] += 1
        by_lang[r.language] += 1

    def _top(c: Counter[str], n: int = 10) -> str:
        if not c:
            return "  _(none)_"
        return "\n".join(f"  - {k}: {v}" for k, v in c.most_common(n))

    lines = [
        "## Summary",
        "",
        f"- Total turns: **{total}**",
        f"- Passed: **{passed}**",
        f"- Failed: **{len(failed)}**",
        "",
        "### By failure reason",
        _top(by_reason),
        "",
        "### By topic",
        _top(by_topic),
        "",
        "### By language",
        _top(by_lang),
        "",
    ]
    return "\n".join(lines)


def _trace_markdown(trace: list[dict[str, object]]) -> str:
    if not trace:
        return "_(no tools called)_"
    rows = []
    for t in trace:
        args = json.dumps(t.get("args"), ensure_ascii=False)
        summary = t.get("result_summary") or ""
        rows.append(
            f"- **{t.get('name')}** · status=`{t.get('status')}` · "
            f"latency={t.get('latency_ms')}ms · args=`{args}` · {summary}"
        )
    return "\n".join(rows)


def _failure_section(index: int, row: FuzzRow) -> str:
    reasons: list[str] = list(row.errors)
    if row.judge:
        reasons.extend(row.judge.failure_reasons)
    reasons_str = ", ".join(f"`{r}`" for r in reasons) or "_(no tags)_"
    judge_summary = row.judge.summary if row.judge else "(judge did not run)"
    scores: list[str] = []
    if row.judge:
        for field in (
            "intent_match",
            "language_ok",
            "tool_choice_ok",
            "factual_vs_trace",
            "coherence",
        ):
            scores.append(f"{field}={getattr(row.judge, field)}")
    scores_str = " · ".join(scores) if scores else "_(no scores)_"

    body = [
        f"## Failure {index}",
        "",
        f"- **run_id**: `{row.run_id}`",
        f"- **ts**: `{row.ts}`",
        f"- **persona**: `{row.persona}`",
        f"- **language**: `{row.language}`",
        f"- **topic**: `{row.topic}`",
        f"- **elapsed_ms**: `{row.elapsed_ms}`",
        f"- **reasons**: {reasons_str}",
        f"- **scores**: {scores_str}",
        f"- **judge summary**: {judge_summary}",
        "",
        "### Question",
        "",
        "```",
        row.question,
        "```",
        "",
        "### Agent reply",
        "",
        "```",
        row.reply or "(empty)",
        "```",
        "",
        "### Tool trace",
        "",
        _trace_markdown(row.tool_trace),
        "",
    ]
    if row.errors:
        body.extend(
            [
                "### Pipeline errors",
                "",
                "```",
                "\n".join(row.errors),
                "```",
                "",
            ]
        )
    body.extend(
        [
            "### Raw row (JSON)",
            "",
            "```json",
            row.model_dump_json(indent=2),
            "```",
            "",
        ]
    )
    return "\n".join(body)


def render_report(
    rows: Sequence[FuzzRow],
    *,
    only_failures: bool = True,
    max_failures: int | None = None,
) -> str:
    """Render a Markdown report suitable for pasting into Claude / Gemini."""
    failures = [r for r in rows if r.failed]
    if max_failures is not None:
        failures = failures[:max_failures]
    sections = [_BANNER, _summary_counts(rows)]
    if only_failures:
        for i, row in enumerate(failures, 1):
            sections.append(_failure_section(i, row))
    else:
        for i, row in enumerate(rows, 1):
            sections.append(_failure_section(i, row))
    return "\n".join(sections)
