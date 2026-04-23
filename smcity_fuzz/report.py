"""Failure-focused report generator.

`summarise()` consumes a list of FuzzRows and returns a human-readable
Markdown / text report grouped by dataset / language / persona /
failure_reason. Each failure carries the judge's one-sentence summary so
a human can triage fast without reading every row.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from smcity_fuzz.store import FuzzRow


def _counter_table(title: str, counts: Counter[str]) -> str:
    if not counts:
        return f"{title}: (none)\n"
    lines = [f"{title}:"]
    for key, n in counts.most_common():
        lines.append(f"  {n:>3}  {key}")
    return "\n".join(lines) + "\n"


def summarise(rows: Sequence[FuzzRow], *, max_failures_shown: int = 15) -> str:
    total = len(rows)
    failed = [r for r in rows if r.failed]
    passed = total - len(failed)

    by_topic: Counter[str] = Counter()
    by_language: Counter[str] = Counter()
    by_persona: Counter[str] = Counter()
    by_reason: Counter[str] = Counter()
    for r in failed:
        by_topic[r.topic] += 1
        by_language[r.language] += 1
        by_persona[r.persona] += 1
        if r.judge:
            for tag in r.judge.failure_reasons:
                by_reason[tag] += 1
        for err in r.errors:
            # 'synth:...' / 'agent_http:...' / 'judge:...' — keep the prefix.
            by_reason[err.split(":", 1)[0]] += 1

    out = []
    out.append("=== Fuzz run summary ===")
    if rows:
        out.append(f"run_id (first row): {rows[0].run_id}")
    out.append(f"total: {total}   passed: {passed}   failed: {len(failed)}")
    out.append("")
    out.append(_counter_table("By failure reason", by_reason))
    out.append(_counter_table("By dataset", by_topic))
    out.append(_counter_table("By language", by_language))
    out.append(_counter_table("By persona", by_persona))
    out.append("")

    if failed:
        out.append(f"=== Top {min(max_failures_shown, len(failed))} failures ===")
        for i, r in enumerate(failed[:max_failures_shown], 1):
            reasons = []
            if r.judge:
                reasons.extend(r.judge.failure_reasons)
            reasons.extend(err.split(":", 1)[0] for err in r.errors)
            reasons_str = ", ".join(reasons) if reasons else "(no tags)"
            summary = r.judge.summary if r.judge else "(no verdict)"
            out.append(
                f"[{i:>2}] persona={r.persona} lang={r.language} topic={r.topic} "
                f"reasons={reasons_str}"
            )
            out.append(f"     Q: {r.question[:160]}")
            reply_short = (r.reply or "").replace("\n", " ")[:200]
            out.append(f"     R: {reply_short}")
            out.append(f"     Judge: {summary}")
            if r.errors:
                out.append(f"     Errors: {'; '.join(r.errors)[:200]}")
            out.append("")

    return "\n".join(out)
