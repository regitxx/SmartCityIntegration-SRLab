#!/usr/bin/env bash
# Re-judge the existing v1 (v0.4.16 baseline) coverage results with the new
# contract-based judge from v0.5.0, so we get an honest before/after on the
# SAME 9 442 question pool — no new LLM calls needed.
#
# Usage (on Mac Studio):
#   ./scripts/rejudge_v1.sh                          # default paths
#   ./scripts/rejudge_v1.sh logs/coverage_results_v1.jsonl

set -euo pipefail

cd "$(dirname "$0")/.."

RESULTS="${1:-logs/coverage_results_v1.jsonl}"
OUT_MD="logs/coverage_report_v1_rejudged.md"
OUT_JSON="logs/coverage_summary_v1_rejudged.json"

if [[ ! -f "$RESULTS" ]]; then
  echo "results file not found: $RESULTS" >&2
  exit 1
fi

echo "==> re-judging $(wc -l < "$RESULTS") v1 rows with v0.5.0 contracts judge"
uv run python -m smcity_fuzz coverage report \
  --results "$RESULTS" \
  --out-md "$OUT_MD" \
  --out-json "$OUT_JSON" \
  --title-suffix "v1 rows re-judged with v0.5.0 contracts"

echo
echo "report:  $OUT_MD"
echo "summary: $OUT_JSON"
echo
echo "diff against v0.4.x summary (if available):"
echo "  diff <(jq -S . logs/coverage_summary_v1.json) <(jq -S . $OUT_JSON) | head -80"
