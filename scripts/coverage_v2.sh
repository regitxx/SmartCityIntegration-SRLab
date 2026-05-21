#!/usr/bin/env bash
# Coverage suite v2 runner (v0.5.0 — multilingual + contracts judge).
#
# One command end-to-end on the Mac Studio host:
#   1. Generate 10 000 questions stratified over en / yue / zh-Hant / zh-Hans
#      via Gemma 4 (`gemma-synth` in LM Studio).
#   2. Drive them through the deployed agent at https://smcity.taila366aa.ts.net.
#   3. Build the markdown + JSON report using the new contract-based judge.
#   4. Copy the JSON summary into /app/state (read by /coverage live).
#
# Resumable: each step appends to its output file. Re-running the script
# after a crash skips already-done questions.
#
# Usage:
#   ./scripts/coverage_v2.sh                              # default 10k
#   ./scripts/coverage_v2.sh --count 1000                 # smaller smoke
#   ./scripts/coverage_v2.sh --skip-generate              # reuse v2 corpus
#   ./scripts/coverage_v2.sh --languages en,yue           # subset of langs

set -euo pipefail

cd "$(dirname "$0")/.."

# --- defaults -------------------------------------------------------------
COUNT="10000"
LANGUAGES="en,yue,zh-Hant,zh-Hans"
CONCURRENCY="4"
AGENT_URL="https://smcity.taila366aa.ts.net"
LM_BASE_URL="http://host.docker.internal:1234/v1"
QUESTIONS="logs/coverage_questions_v2.jsonl"
RESULTS="logs/coverage_results_v2.jsonl"
REPORT_MD="logs/coverage_report_v2.md"
SUMMARY_JSON="data/coverage_test_summary.json"
PROD_SUMMARY="/app/state/coverage_test_summary.json"   # docker volume mount path
SKIP_GENERATE=0
SKIP_RUN=0
TITLE_SUFFIX="10k v2 (multilingual + v0.5.0 architecture)"

# --- arg parsing ----------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --count)        COUNT="$2"; shift 2 ;;
    --languages)    LANGUAGES="$2"; shift 2 ;;
    --concurrency)  CONCURRENCY="$2"; shift 2 ;;
    --agent-url)    AGENT_URL="$2"; shift 2 ;;
    --lm-base-url)  LM_BASE_URL="$2"; shift 2 ;;
    --skip-generate) SKIP_GENERATE=1; shift ;;
    --skip-run)     SKIP_RUN=1; shift ;;
    --title-suffix) TITLE_SUFFIX="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,/^$/p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 64 ;;
  esac
done

mkdir -p logs data

# --- step 1: generate -----------------------------------------------------
if [[ "$SKIP_GENERATE" -eq 0 ]]; then
  echo "==> step 1/3: generate $COUNT questions ($LANGUAGES) via gemma-synth"
  uv run python -m smcity_fuzz coverage generate \
    --count "$COUNT" \
    --gemma-model gemma-synth \
    --languages "$LANGUAGES" \
    --lm-base-url "$LM_BASE_URL" \
    --out "$QUESTIONS"
else
  echo "==> step 1/3: SKIPPED (reusing $QUESTIONS)"
fi
echo "    corpus: $(wc -l < "$QUESTIONS") questions"

# --- step 2: run through the agent ---------------------------------------
if [[ "$SKIP_RUN" -eq 0 ]]; then
  echo "==> step 2/3: run through agent at $AGENT_URL (concurrency=$CONCURRENCY)"
  uv run python -m smcity_fuzz coverage run \
    --questions "$QUESTIONS" \
    --agent-url "$AGENT_URL" \
    --concurrency "$CONCURRENCY" \
    --out "$RESULTS"
else
  echo "==> step 2/3: SKIPPED (reusing $RESULTS)"
fi
echo "    results: $(wc -l < "$RESULTS") rows"

# --- step 3: build report ------------------------------------------------
echo "==> step 3/3: build report (contract-based judge)"
uv run python -m smcity_fuzz coverage report \
  --results "$RESULTS" \
  --out-md "$REPORT_MD" \
  --out-json "$SUMMARY_JSON" \
  --title-suffix "$TITLE_SUFFIX"

# --- step 4: publish summary to the live agent's /app/state -------------
# The /coverage page reads coverage_test_summary.json from /app/state. Copy
# into BOTH replicas' shared volume so the UI updates without an agent
# restart.
if docker volume inspect smcity-sessions >/dev/null 2>&1; then
  echo "==> publishing summary into smcity-sessions docker volume"
  # docker cp to a stopped/anon container that mounts the volume
  docker run --rm -v smcity-sessions:/app/state -v "$(pwd)/$SUMMARY_JSON:/tmp/summary.json:ro" \
    alpine sh -c "cp /tmp/summary.json /app/state/coverage_test_summary.json && echo published"
else
  echo "==> (skipping volume publish — smcity-sessions docker volume not found)"
fi

echo
echo "report:  $REPORT_MD"
echo "summary: $SUMMARY_JSON"
echo "live at: $AGENT_URL/coverage"
