"""Coverage suite analyzer + markdown reporter.

Reads the JSONL output of coverage_run.py, reconciles each turn against
the coverage_catalog (which dataset was the question generated FOR and
which tools SHOULD have fired) and writes:

  * a markdown report grouped by dataset with per-row success/failure
    counts and sample failing questions,
  * a JSON summary file ready to feed the /coverage page's "tested
    coverage" section.

The match logic is intentionally lenient: a turn is "covered" if any of
the `expected_tools` for its target dataset appears in the actual
tool_trace. We bucket the rest of the turns by failure mode so the
report tells you WHY coverage is missing — not just THAT it is.

Usage::

    python -m smcity_fuzz coverage report \\
        --results logs/coverage_results_v1.jsonl \\
        --out-md  logs/coverage_report_v1.md \\
        --out-json logs/coverage_summary_v1.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _read_results(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                out.append(json.loads(stripped))
            except json.JSONDecodeError:
                continue
    return out


_FAILURE_LABELS: dict[str, str] = {
    "ok_expected_tool": "ok — expected tool fired",
    "ok_other_tool": "ok — different tool fired",
    "ok_no_tool": "ok — agent answered without a tool (chitchat / fast-path / cached)",
    "error_status": "agent returned status != ok",
    "timeout": "request timed out",
    "http_error": "HTTP non-200 from /turn",
    "network_error": "network / connection failure",
    "empty_reply": "agent replied with empty text",
    "geocoder_collision": "geocoder collision (origin = destination)",
}


@dataclass
class DatasetReport:
    dataset_id: str
    total: int = 0
    buckets: Counter[str] = field(default_factory=Counter)
    sample_failures: list[dict[str, Any]] = field(default_factory=list)
    expected_tool_hit_rate: float = 0.0
    avg_elapsed_ms: float = 0.0
    tool_fire_counts: Counter[str] = field(default_factory=Counter)


def _categorise(row: dict[str, Any]) -> str:
    """Bucket a result row into a single failure-mode label."""
    if row.get("status") == "timeout":
        return "timeout"
    if row.get("status") == "http_error":
        return "http_error"
    if row.get("status") == "network_error":
        return "network_error"
    if row.get("status") != "ok":
        return "error_status"
    reply = (row.get("reply_text") or "").strip()
    trace = row.get("tool_trace") or []
    expected = set(row.get("expected_tools") or [])
    fired = {t["name"] for t in trace if isinstance(t, dict)}
    # Geocoder collision shows up as a 'resolved to nearly the same' phrase
    # in the reply (the collision guard) — bucket explicitly.
    if "resolved to nearly the same" in reply:
        return "geocoder_collision"
    if not reply:
        return "empty_reply"
    if expected & fired:
        return "ok_expected_tool"
    if fired:
        return "ok_other_tool"
    return "ok_no_tool"


def analyse(results: Sequence[dict[str, Any]]) -> dict[str, DatasetReport]:
    by_dataset: dict[str, DatasetReport] = defaultdict(
        lambda: DatasetReport(dataset_id="")
    )
    for row in results:
        ds_id = row.get("expected_dataset_id") or "<unknown>"
        report = by_dataset[ds_id]
        report.dataset_id = ds_id
        report.total += 1
        bucket = _categorise(row)
        report.buckets[bucket] += 1
        if not bucket.startswith("ok_") and len(report.sample_failures) < 5:
            report.sample_failures.append(
                {
                    "bucket": bucket,
                    "question": row.get("question_en", "")[:200],
                    "reply": (row.get("reply_text") or "")[:200],
                    "tools_fired": [t["name"] for t in row.get("tool_trace") or []],
                }
            )
        elapsed = row.get("elapsed_ms") or 0
        # Compute rolling average without storing every elapsed.
        report.avg_elapsed_ms = (
            (report.avg_elapsed_ms * (report.total - 1) + elapsed) / report.total
        )
        for t in row.get("tool_trace") or []:
            if isinstance(t, dict) and t.get("name"):
                report.tool_fire_counts[t["name"]] += 1

    for r in by_dataset.values():
        n_ok_expected = r.buckets.get("ok_expected_tool", 0)
        r.expected_tool_hit_rate = n_ok_expected / r.total if r.total else 0.0

    return dict(by_dataset)


def render_markdown(
    by_dataset: dict[str, DatasetReport],
    *,
    catalog_path: Path | None,
    title_suffix: str = "",
) -> str:
    catalog_title: dict[str, str] = {}
    catalog_category: dict[str, str] = {}
    if catalog_path and catalog_path.exists():
        cat = json.loads(catalog_path.read_text(encoding="utf-8"))
        for entry in cat.get("datasets", []):
            catalog_title[entry["id"]] = entry["title"]
            catalog_category[entry["id"]] = entry["category"]
        for entry in cat.get("additional_integrations", []):
            slug = f"X-{entry['title'][:20].replace(' ', '_')}"
            catalog_title[slug] = entry["title"]
            catalog_category[slug] = entry["category"]

    total = sum(r.total for r in by_dataset.values())
    by_bucket: Counter[str] = Counter()
    for r in by_dataset.values():
        by_bucket.update(r.buckets)

    lines: list[str] = []
    suffix = f" — {title_suffix}" if title_suffix else ""
    lines.append(f"# Coverage Test Report{suffix}")
    lines.append("")
    lines.append(f"_generated {datetime.now(UTC).isoformat()}_")
    lines.append("")
    lines.append("## Overall")
    lines.append("")
    lines.append(f"- Total questions run: **{total}**")
    lines.append("")
    lines.append("| bucket | count | share |")
    lines.append("|---|---:|---:|")
    for bucket, count in by_bucket.most_common():
        share = count / total if total else 0
        lines.append(
            f"| {_FAILURE_LABELS.get(bucket, bucket)} | {count} | {share:.1%} |"
        )
    lines.append("")
    lines.append("## Per-dataset breakdown")
    lines.append("")
    lines.append(
        "| dataset | title | total | ok (expected) | ok (other) | no-tool | "
        "errors | timeouts | hit rate | avg ms |"
    )
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for ds_id in sorted(by_dataset.keys()):
        r = by_dataset[ds_id]
        title = catalog_title.get(ds_id, "")
        errs = (
            r.buckets.get("error_status", 0)
            + r.buckets.get("http_error", 0)
            + r.buckets.get("network_error", 0)
            + r.buckets.get("empty_reply", 0)
            + r.buckets.get("geocoder_collision", 0)
        )
        lines.append(
            f"| `{ds_id}` | {title} | {r.total} | "
            f"{r.buckets.get('ok_expected_tool', 0)} | "
            f"{r.buckets.get('ok_other_tool', 0)} | "
            f"{r.buckets.get('ok_no_tool', 0)} | "
            f"{errs} | "
            f"{r.buckets.get('timeout', 0)} | "
            f"{r.expected_tool_hit_rate:.0%} | "
            f"{r.avg_elapsed_ms:.0f} |"
        )
    lines.append("")
    lines.append("## Failure samples (up to 5 per dataset)")
    lines.append("")
    for ds_id in sorted(by_dataset.keys()):
        r = by_dataset[ds_id]
        if not r.sample_failures:
            continue
        title = catalog_title.get(ds_id, "")
        lines.append(f"### `{ds_id}` — {title}")
        lines.append("")
        for f in r.sample_failures:
            lines.append(f"- **[{f['bucket']}]** Q: {f['question']!r}")
            lines.append(f"  - tools fired: `{f['tools_fired']}`")
            if f.get("reply"):
                lines.append(f"  - reply: {f['reply']!r}")
        lines.append("")
    return "\n".join(lines)


def render_summary_json(by_dataset: dict[str, DatasetReport]) -> dict[str, Any]:
    """Compact JSON ready for the /coverage UI to consume."""
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "datasets": [
            {
                "dataset_id": r.dataset_id,
                "total": r.total,
                "expected_tool_hit_rate": round(r.expected_tool_hit_rate, 4),
                "avg_elapsed_ms": round(r.avg_elapsed_ms),
                "buckets": dict(r.buckets),
                "top_tools_fired": r.tool_fire_counts.most_common(5),
            }
            for r in sorted(by_dataset.values(), key=lambda r: r.dataset_id)
        ],
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="smcity_fuzz coverage report")
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--out-md", type=Path, required=True)
    parser.add_argument("--out-json", type=Path, default=None)
    parser.add_argument(
        "--catalog",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data" / "coverage_catalog.json",
    )
    parser.add_argument("--title-suffix", default="")
    args = parser.parse_args(list(argv) if argv is not None else None)

    results = _read_results(args.results)
    by_dataset = analyse(results)
    md = render_markdown(by_dataset, catalog_path=args.catalog, title_suffix=args.title_suffix)
    args.out_md.parent.mkdir(parents=True, exist_ok=True)
    args.out_md.write_text(md, encoding="utf-8")

    if args.out_json:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(
            json.dumps(render_summary_json(by_dataset), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    print(
        f"wrote {args.out_md} ({len(results)} results, {len(by_dataset)} datasets)",
        file=sys.stderr,
    )
    return 0


__all__ = ["analyse", "main", "render_markdown", "render_summary_json"]


if __name__ == "__main__":
    sys.exit(main())
