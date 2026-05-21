"""Coverage suite analyzer + markdown reporter.

Reads the JSONL output of coverage_run.py and runs each row through
`smcity_fuzz.contracts.evaluate`, which returns a semantic verdict
(`complete` / `partial_chain` / `wrong_tool` / `no_tool` / error
variants) rather than the old string-intersection check.

Outputs:
  * a markdown report grouped by dataset with per-row success/failure
    counts and sample failing questions,
  * a JSON summary file ready to feed the /coverage page's "tested
    coverage" section.

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

from smcity_fuzz.contracts import BUCKET_LABELS, OK_BUCKETS, evaluate


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


_FAILURE_LABELS: dict[str, str] = dict(BUCKET_LABELS)


@dataclass
class DatasetReport:
    dataset_id: str
    total: int = 0
    buckets: Counter[str] = field(default_factory=Counter)
    sample_failures: list[dict[str, Any]] = field(default_factory=list)
    complete_rate: float = 0.0
    avg_elapsed_ms: float = 0.0
    tool_fire_counts: Counter[str] = field(default_factory=Counter)


def analyse(results: Sequence[dict[str, Any]]) -> dict[str, DatasetReport]:
    by_dataset: dict[str, DatasetReport] = defaultdict(
        lambda: DatasetReport(dataset_id="")
    )
    for row in results:
        ds_id = row.get("expected_dataset_id") or "<unknown>"
        report = by_dataset[ds_id]
        report.dataset_id = ds_id
        report.total += 1
        verdict = evaluate(row)
        bucket = verdict.bucket
        report.buckets[bucket] += 1
        if bucket not in OK_BUCKETS and len(report.sample_failures) < 5:
            report.sample_failures.append(
                {
                    "bucket": bucket,
                    "reason": verdict.reason,
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
        n_complete = sum(r.buckets.get(b, 0) for b in OK_BUCKETS)
        r.complete_rate = n_complete / r.total if r.total else 0.0

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
        "| dataset | title | total | complete | partial | wrong-tool | no-tool | "
        "errors | timeouts | complete % | avg ms |"
    )
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for ds_id in sorted(by_dataset.keys()):
        r = by_dataset[ds_id]
        title = catalog_title.get(ds_id, "")
        errs = (
            r.buckets.get("error_status", 0)
            + r.buckets.get("http_error", 0)
            + r.buckets.get("network_error", 0)
            + r.buckets.get("empty_reply", 0)
            + r.buckets.get("geocoder_collision", 0)
            + r.buckets.get("unknown_dataset", 0)
        )
        lines.append(
            f"| `{ds_id}` | {title} | {r.total} | "
            f"{r.buckets.get('complete', 0)} | "
            f"{r.buckets.get('partial_chain', 0)} | "
            f"{r.buckets.get('wrong_tool', 0)} | "
            f"{r.buckets.get('no_tool', 0)} | "
            f"{errs} | "
            f"{r.buckets.get('timeout', 0)} | "
            f"{r.complete_rate:.0%} | "
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
            if f.get("reason"):
                lines.append(f"  - reason: {f['reason']}")
            lines.append(f"  - tools fired: `{f['tools_fired']}`")
            if f.get("reply"):
                lines.append(f"  - reply: {f['reply']!r}")
        lines.append("")
    return "\n".join(lines)


def render_summary_json(by_dataset: dict[str, DatasetReport]) -> dict[str, Any]:
    """Compact JSON ready for the /coverage UI to consume.

    Both `complete_rate` (new, contract-based) and `expected_tool_hit_rate`
    (kept for one release of backward-compatibility with the existing
    /coverage page) are emitted; the UI should migrate to `complete_rate`.
    """
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "datasets": [
            {
                "dataset_id": r.dataset_id,
                "total": r.total,
                "complete_rate": round(r.complete_rate, 4),
                "expected_tool_hit_rate": round(r.complete_rate, 4),  # alias for v0.4.x UI
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
