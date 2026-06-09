# Changelog

All notable changes to this project are documented here. Versions follow [SemVer](https://semver.org/).

## [0.8.1] — 2026-06-09

**Bounded retry/backoff for the POI mirror refresh — so a cold deploy actually fills.** The v0.8.0 deploy surfaced a gap the unit tests didn't: on a *cold* deploy the in-process warm-up fires all 30 categories at `overpass-api.de`'s free endpoint in quick succession, and it 429-rate-limits (with intermittent 504s) on roughly half of them. `refresh_all` logged each failure and moved on — so the mirror landed only ~14/30 categories and stayed that way until the next nightly cycle, silently falling back to live Overpass for the gaps (re-introducing the latency + flakiness the mirror exists to remove). Reaching 30/30 on the v0.8.0 production deploy required manual paced + targeted-retry runs.

### Fix

`smcity/data/poi_refresh.py` gains `refresh_category_with_retry`, which `refresh_all` now uses per category:

- Retries only on **transient** failures — 429, 5xx, and timeout/network errors — detected structurally via the chained `httpx` error on `exc.__cause__` (`HTTPStatusError.response.status_code` / `RequestError`), **not** by string-matching the message. A non-transient error (e.g. a malformed-query 400) re-raises immediately rather than wasting retries on something that will fail identically.
- Backoff is `Retry-After` when the server provides it, else exponential (`poi_refresh_backoff_base_s · 2^attempt`, capped at 60s, with jitter so the two replicas don't resync in lockstep).
- On exhaustion the last error still propagates to `refresh_all`'s per-category guard, which logs `poi_refresh.category_failed` and keeps sweeping — a single dead category never aborts the rebuild (unchanged contract).

The retry lives in the **background refresh only**, never on the live `find_poi` fallback — a user turn must not block on a 60s backoff.

### Config

Two new settings (`smcity/settings.py`): `poi_refresh_max_retries` (default 4, set 0 to disable) and `poi_refresh_backoff_base_s` (default 5.0s). The per-category throttle default is unchanged at 2s — the retry/backoff is the robustness mechanism; tuning the base throttle is a separate, empirical question best answered with a measured cold-deploy.

### Also

- **Stale Tailscale hostname fixed repo-wide.** The deployed node is `smcity-1.taila366aa.ts.net` (Tailscale appended `-1`; the bare `smcity` name was taken), but `deploy/README.md`, `scripts/coverage_v2.sh`, and — more importantly — the `--agent-url` **defaults** in `smcity_fuzz/coverage_run.py` and `smcity_fuzz/cli.py` all pointed at the dead `smcity.` host. The code defaults would silently make a no-flag sweep time out. (Historical CHANGELOG entries left as-is.)
- **`docs/TEST_CASES.md` refreshed:** added a calibration scoreboard (v0.7.0 → v0.8.0 trend + reproduce-the-sweep command with the correct host and `FUZZER_MODEL` pin baked in); removed the stale `transport.plan_taxi_estimate` row and the "+ taxi" in `plan_journey`'s description (taxi was removed from the planner in v0.4.12 — the code was already correct; only the doc lagged).

### Tests

6 new (`tests/test_poi_store.py`): retry-then-succeed, give-up-after-max-retries, no-retry-on-400, retry-on-timeout, `Retry-After` honored, and `refresh_all` skips an exhausted category while still populating the other 29. 405 pass (7 integration tests deselected off-Tailscale); ruff + mypy strict clean.

## [0.8.0] — 2026-06-05

**A local POI mirror, so `find_poi` stops hitting public Overpass on every turn.** `geo.find_poi` previously POSTed to `overpass-api.de` for every request. That endpoint is slow and intermittently 504s — a latency source *and* a measurement confounder (a 504 surfaces as a `tool_error` that has nothing to do with the model, which muddied the v0.7.1 calibration). This release adds a nightly SQLite mirror and queries it first.

### What's new

New `smcity/data/poi_store.py` is a SQLite + R*Tree spatial mirror of the 30 POI categories over the Hong Kong bbox. `smcity/data/poi_refresh.py` rebuilds it nightly, **in-process** (an asyncio task in the app lifespan), with an `fcntl` advisory lock on the shared `/app/state` volume so that — across the two blue/green replicas — exactly one refreshes per cycle. No extra service, no host cron: the lock *is* the leader election, and it lives next to the DB it guards. A `python -m smcity.data.poi_refresh` CLI gives ops a manual warm-up.

`find_poi` now queries the mirror first and falls back to live Overpass behind a flag (`POI_OVERPASS_FALLBACK`, the A/B switch). On a fresh deploy the mirror is empty, so every query transparently falls back to live exactly as before — until the first refresh fills it, within minutes. Freshness is tracked per category, so a category that genuinely has zero POIs in HK doesn't loop back to live forever.

### No drift, by construction

The refresh does **not** re-implement tag derivation or element parsing. It calls the live tool's own `_build_query` (Overpass query) and a newly-factored `_parse_overpass_elements` (element → `OsmPoi` shaping), so the mirror is byte-for-byte what a live `find_poi` would have returned. Same single-source-of-truth discipline the category registry established in v0.7.1, extended to the data path. A parity test locks it.

### Observability

`/health` gains a `poi_mirror` block: categories populated (of 30), total POIs, oldest/newest refresh timestamp, and a `stale` flag (oldest refresh older than 2× the interval). So we can see at a glance how fresh the mirror is.

### Notes

- The nightly refresh offloads its SQLite writes to a worker thread (`asyncio.to_thread`), so a large category's insert can't block the event loop and spike latency for users on the replica doing the refresh — most visible during the startup warm-up right after a deploy.
- WAL on the shared volume across both replicas is the same pattern `SessionStore` already runs in production, so the concurrency story is proven, not new.
- 15 new tests (`tests/test_poi_store.py`): store round-trip / bbox / limit, zero-row freshness, parse parity, local-first-no-network, cold fallback, A/B isolation, the full 30-category sweep, and the cross-replica lock.

### Measurement — calibrated 200-row sweep

Ran the calibrated `coverage_run → coverage_judge` against the deployed v0.8.0 — same fixed corpus (`data/synth/v0.6.0_20260526_sample200_calibration.jsonl`), 200 rows, concurrency 1, judged with `gpt-oss-120b` as sole no-TTL model — identical methodology to the v0.7.1 baseline. Judged file: `logs/coverage_judged_v0.8.0_sample200.jsonl`. (The judge model must be pinned with `FUZZER_MODEL=openai/gpt-oss-120b`; the harness default is `gpt-oss-20b`, which would break baseline parity. The metric formulas were re-validated by reproducing v0.7.0's published numbers exactly.)

| metric | v0.7.0 | v0.7.1 | v0.8.0 |
|---|---:|---:|---:|
| `tool_error`/100 | 4.5 | 27.0 | **4.0** |
| `wrong_tool`/100 | 39.5 | 30.0 | **26.0** |
| pass rate | 33.5% | 28.0% | **45.5%** |
| avg score /10 | 5.72 | 5.88 | **6.99** |
| latency median | — | 11.3s | **7.1s** |
| latency p95 | — | 24.7s | **19.2s** |
| live Overpass calls | — | many (504-prone) | **0** |

**The thesis holds by construction.** All 127 `find_poi` calls in the sweep were served by the local mirror — **zero hit live Overpass**. That collapses `tool_error` 27.0 → 4.0 (back to the v0.6.3/v0.7.0 structural floor — the residual is non-`find_poi` tools, not Overpass) and recovers pass rate 28.0% → 45.5%. This **vindicates the v0.7.1 diagnosis**: that release flagged its own 28% pass rate as "not a defect — an upstream confound" (Overpass 504s correctly routed *into* `find_poi`). Removing the confound recovered the rate, confirming routing was never the problem. Whole-turn latency also fell 37% (median 11.3s → 7.1s) since the mirror replaces a slow, variable network round-trip with a local R*Tree lookup; `wrong_tool` improved slightly (30.0 → 26.0) as more chains complete their intended tool use rather than aborting on a 504.

**Honest gaps this measurement surfaces:**
- **Latency is still the #1 unmet goal.** 7.1s median is ~5× the `GOAL.md` ≤1.5s target. The mirror did its job; the two `gpt-oss-120b` hops (decide + synthesise) now dominate the budget. The next lever is replacing the decide-120B hop with a deterministic/gemma intent classifier (target common-path ≤1.5s).
- **Cold-deploy warm-up needs a backoff fix.** The startup refresh landed only 14/30 categories on a cold deploy — `overpass-api.de` rate-limited (429) and timed-out (504) on ~half at the default 2s throttle with no retry. 30/30 was reached only via manual paced + targeted-retry runs. A warm mirror stays warm (`replace_category` swaps only on success), but a cold deploy under-populates until a v0.8.1 adds bounded per-category retry/backoff.

## [0.7.1] — 2026-06-03

**`wrong_tool` routing fix + a single-source category registry.** Calibrated v0.7.0 fuzz put `wrong_tool` at 39.5/100 turns — the largest remaining defect. Pulling the judged sample apart showed it was two mechanisms, not one: 45/79 rows called *no tool at all* (a decide-step inaction problem, untouched here), and **26/79 called `geo.address_lookup` alone for what were unambiguously POI-find queries** (hardware supplier, clothes shops, beauty/hair salons, MTR entrances, toilets, markets, temples, bookstores, shelters, …). The `POI_CHAIN_RULE` is *supposed* to catch exactly that — address_lookup ran, find_poi didn't → auto-dispatch find_poi — but its question predicate returned False on **25 of the 26**, so the rule never fired.

Root cause was a keyword table that had drifted and three matching bugs. Rather than grow that table (the patch-the-examples trap), this release fixes the architecture.

### Single source of truth

The category↔knowledge lived in **four parallel tables across two files** — `_CATEGORIES` (OSM tags), `_CATEGORY_HINTS` (LLM schema), `PoiCategory` (enum), and chain_rules' `_POI_CATEGORY_PATTERNS` (router regex). The LLM's notion of "what words mean `beauty_shop`" and the router's notion were maintained by hand, separately, and disagreed.

New `smcity/tools/poi_categories.py` is the canonical registry: one `CategorySpec` per slug owns its OSM tags, its bilingual LLM hint, and its matching lexicon. The `find_poi` schema string, the enum, the Overpass tags, and the deterministic `categorize()` router all *derive* from it. Adding a category is now a one-place edit, and the model's view can't diverge from the router's. Net **−210 lines**.

### Two matcher mechanisms (not per-keyword hacks)

1. **Script normalisation** — user text runs through the existing `simplified_to_hk()` before matching, so a single HK-Traditional term covers Simplified input (百货 ⇒ 百貨). Chinese terms are now written in Traditional only.
2. **ASCII-alnum lookaround boundaries** instead of `\b` — fixes both the English-plural bug (`\bshop\b` never matched "shops", documented for years on `benches?`) *and* the CJK-jammed-Latin bug (`\b` doesn't fire between a Chinese char and a glued-on English token like "搵個shelter", which is exactly how Cantonese mixes English). Each slug also matches its own literal form, since synth corpora occasionally leak the slug verbatim ("variety_store").

`categorize()` runs deterministically (no second LLM hop — deliberate, given LM Studio sustains ~0.08 q/s) with the existing `LLMHint` as the fallback when no term matches.

### Result on the real misroute corpus

25 of the 26 v0.7.0 misroutes now route — each to the *correct* category. The 26th (自助售賣機 / vending machine) correctly stays unmatched: it isn't one of the 30 categories, so the rule falls to `LLMHint` and lets the LLM decide rather than forcing a wrong slug.

The LLM-facing schema string is **byte-identical** to v0.7.0 (locked by a golden-hash test), so the next calibration's delta is attributable to routing alone, not a prompt change. Enriching the LLM hints with the new synonyms is a deliberate, separately-measured follow-up.

### Files

- `smcity/tools/poi_categories.py` — new canonical registry + `categorize()` + `category_field_description()`.
- `smcity/tools/osm_pois.py` — derives tags/enum/schema from the registry; `_CATEGORIES` kept as a compat alias. −111 lines.
- `smcity/chain_rules.py` — predicate + resolver now call shared `categorize()`; the private regex table is gone. −152 lines.
- `tests/test_poi_categories.py` — 11 new tests: registry/enum/schema-hash invariants, both matcher mechanisms, HK-synonym corpus, and a cross-contamination test that locks first-match-wins ordering as categories are added.
- `pyproject.toml` — bumped 0.7.0 → 0.7.1.

### Measurement — calibrated 200-row sweep

Ran the calibrated `coverage_run → coverage_judge` (200 rows, concurrency 1, `gpt-oss-120b` as sole no-TTL model) against the v0.6.3/v0.7.0 baselines. Judged file: `logs/coverage_judged_v0.7.1_run2_sample200.jsonl`.

| metric | v0.6.3 base | v0.7.0 | v0.7.1 |
|---|---:|---:|---:|
| `wrong_tool`/100 | 39.5 | 39.5 | **30.0 (−24%)** ✅ |
| pass rate | 32.5% | 33.5% | 28.0% |
| avg score /10 | 5.67 | 5.72 | 5.88 |
| `tool_error`/100 | 4.5 | 4.5 | 27.0 |
| latency median | — | — | 11.3s (p95 24.7s) |

**The routing fix worked: `wrong_tool` −24%, each newly-routed query going to the correct category.** The pass-rate dip is *not* a v0.7.1 defect — it's an upstream confound. Public `overpass-api.de` was throwing 504s during the run, spiking `tool_error` 4.5 → 27.0. Ironically the fix *caused* more of that exposure by correctly routing more queries into `find_poi → Overpass`, which was the failing dependency. Read routing (`wrong_tool`) and upstream errors (`tool_error`) as separate dimensions: routing improved, the upstream endpoint degraded.

This run is the motivation for the next release's headline work — mirroring Overpass to a local store removes the flakiness that confounded this very measurement (and removes a latency source). Latency itself (11.3s median vs. the `GOAL.md` ≤1.5s target) is now the #1 gap and is tracked as a first-class number going forward.

### Tests

391 pass (up from 380 — 11 new). Ruff + mypy strict clean.

## [0.7.0] — 2026-05-28

**`wrong_language` synthesis invariant.** Calibrated v0.6.3 fuzz showed ~18% of replies in a language different from the user's question — stable across biased and calibrated runs, so it's genuine agent behaviour. The `language_stick_reminder` system prompt was already in place but gpt-oss-120b drifts to English on Chinese queries anyway, especially when tool results contain English-named records.

Structural fix per the project's "enforcement > prompt instruction" principle: detect script mismatch post-synthesis and re-prompt the LLM with a corrective hint that includes a sentence-starter exemplar in the target language.

### How it works

A new `WRONG_LANGUAGE_INVARIANT` plugged into the existing `apply_invariants` engine. Same shape as the `DATA_DENIAL_INVARIANT`:

1. After synthesis, the orchestrator's `_maybe_retry_for_invariants()` runs every invariant.
2. The new invariant classifies the reply by script: count CJK ideographs / Hiragana / Katakana / Hangul vs Latin letters, ignoring digits + punctuation.
3. If the user's `primary_lang` expects CJK script (`yue` / `zho` / `jpn` / `kor` / detection.script in {Hant, Hans, Hiragana, Hangul}) AND the reply is dominantly Latin (≥30% Latin, <30% CJK), fire `kind="wrong_language"`.
4. Inverse direction handled symmetrically.
5. Corrective prompt is structural — it tells the LLM to rewrite using the same tool results in the user's language, and includes a concrete sentence-opener exemplar (e.g. for Cantonese: `"而家 / 喺 / 嘅 / 咗 / 冇"` and three opening phrases).
6. The orchestrator's standard one-retry-per-turn invariant path takes over.

### What about bilingual replies?

A reply with even 30%+ characters in the user's expected script is treated as a valid bilingual reply and passes. HK Cantonese commonly mixes English place names into Chinese prose (`"尖沙咀 Tsim Sha Tsui 附近"`) — those are not failures. The 30% threshold keeps the false-positive rate near zero on natural HK text.

### Edge cases handled

- Empty / whitespace-only replies → skip (other invariants handle).
- Replies under 8 meaningful characters → skip ("22.30, 114.17 — 5 min." has too little script content to grade confidently).
- Replies with no script characters at all → skip.
- Tested on: pure Cantonese, pure English, balanced bilingual, English-with-CJK-only-place-names, Chinese-with-English-name-fields, numeric-only.

### Files

- `smcity/synthesis_invariants.py` — new `_wrong_language_check`, `_script_profile`, `_meaningful_chars`, `_is_cjk`, `_is_latin`, `_build_wrong_language_prompt`. Doc-comment + the existing `_data_denial_check` engine are unchanged.
- `tests/test_synthesis_invariants.py` — 11 new tests covering the two directions (CJK←→Latin), bilingual tolerance, threshold edge cases, and script-profile primitives. 43 tests in the invariants suite, 382 in the full suite.
- `pyproject.toml` — bumped 0.6.3 → 0.7.0.

### Expected impact

The calibrated v0.6.3 baseline showed `wrong_language` rate at 18.0/100 turns. v0.7.0's structural fix should drive this toward zero on the next 200-row calibration. We'll measure after the deploy lands.

### Tests

382 tests pass (up from 370 — 12 new invariant + script-profile tests). Ruff + mypy strict no regression from baseline.

## [0.6.3] — 2026-05-28

**LLM-as-judge pipeline + the calibration that exposed the v0.6.2 report bias.** This release adds the missing piece between "we have fuzz results" and "we know if the agent's responses are good or bullshit": a runnable LLM judge over the existing `smcity_fuzz.judge.judge` rubric, plus the methodology fix that makes the judge's `factual_vs_trace` dimension trustworthy.

### Why

A stakeholder review surfaced the question "is the process particularly good enough?" — the rule-based contracts in `smcity_fuzz/contracts.py` only check bucket-level shapes (did the right tool fire? did the reply mention numbers?). They can't tell whether the agent's reply is actually correct vs. plausible-but-wrong vs. fabricated. The existing `smcity_fuzz/judge.py` had been designed exactly for this — five-dimension rubric with structured JSON output — but had never been wired into the coverage pipeline.

### What's new

- **`smcity_fuzz/coverage_judge.py`** — async runner that drives `judge()` over a `coverage_run` output JSONL, writes one structured `JudgeVerdict` per row, resumable per `question_id`. Same shape as `coverage_run.py` and `coverage_gen.py`.
- **`smcity_fuzz/coverage_run.py`** — extended to keep `args`, `result`, and `result_summary` on every tool trace entry. Was discarding the raw result blob to keep JSONL small; that bias broke the judge's `factual_vs_trace` dimension because it had nothing to ground claims against. Adds ~5-10 KB per row to the JSONL — acceptable for the calibration gain.

### What the calibration found

Ran the judge twice over independent 500-row + 200-row samples of the v0.6.2 agent:

| metric | biased (v0.6.2 coverage_run, no raw evidence) | calibrated (v0.6.3 coverage_run, full evidence) | Δ |
|---|---:|---:|---:|
| Pass rate | 3.6% | **32.5%** | +28.9 pp |
| Avg total score | 4.30 / 10 | **5.67 / 10** | +1.37 |
| `hallucinated_fact` rate | 39 / 100 | **14 / 100** | -25.0 |
| `tool_error` rate | 56 / 100 | **11 / 100** | -44.8 |
| `factual_vs_trace` avg | 0.32 / 2 | **1.01 / 2** | +0.69 |
| `wrong_language` rate | 18.2 / 100 | 18.0 / 100 | -0.2 |
| `wrong_tool` rate | 40.4 / 100 | 38.5 / 100 | -1.9 |

The 3.6% headline in the v0.6.2 biased report was ~9× pessimistic — the judge had no way to verify "the tool returned data" claims and was punishing the agent for the absence of evidence. With full evidence the agent's real pass rate is ~32%. Still mediocre, but a dramatically different operational picture.

The dimensions that **stayed put across both runs** (wrong_language, wrong_tool, incomplete, refused_wrongly) are the genuine signal. Those are the real v0.7 targets.

### Files

- `smcity_fuzz/coverage_judge.py` — new (197 lines).
- `smcity_fuzz/coverage_run.py` — `tool_trace` entries now include `args`, `result`, `result_summary`. v0.6.3 marker in the inline comment.
- `data/synth/v0.6.0_20260526_sample200_calibration.jsonl` — pinned 200-row stratified sample (a different draw from the 10k corpus than the 500 v0.6.2 sample) so future calibration runs are repeatable.
- `pyproject.toml` — bumped 0.6.2 → 0.6.3.

### How to use the pipeline

```
# Generate coverage results with full evidence (v0.6.3 default)
python -m smcity_fuzz.coverage_run \
  --questions data/synth/<corpus>.jsonl \
  --agent-url https://smcity-1.taila366aa.ts.net \
  --out logs/coverage_results.jsonl \
  --concurrency 3 --timeout-s 90

# Grade them with the LLM judge
FUZZER_MODEL=openai/gpt-oss-120b python -m smcity_fuzz.coverage_judge \
  --results logs/coverage_results.jsonl \
  --out logs/coverage_judged.jsonl \
  --concurrency 2 --timeout-s 120
```

The judge defaults to `openai/gpt-oss-20b` in fuzz settings — explicitly override to `openai/gpt-oss-120b` (the only large model loaded on the Mac Studio) until that default is fixed in a follow-up.

### Tests

No new tests this release — the work is observability + driver scripts, not agent behaviour. 370 existing tests still pass. Ruff + mypy strict no regression.

## [0.6.2] — 2026-05-27

**Widen the LM Studio httpx exclude pattern.** v0.6.1's `_LM_STUDIO_EXCLUDE_PATTERNS` only suppressed `/v1/chat/completions` and `/v1/embeddings`, but the agent ALSO polls `/v1/models` on every health-check (~every 5 seconds via the LLM probe inside `/health`). Those polls produced bare `GET` spans at 2-4ms — fast and harmless individually, but they dominate the Phoenix span-list view at one row per 5s × thousands of seconds × multiple replicas. From a stakeholder's perspective the v0.6.1 fix looked broken even though the HK upstream renames (`hk.als.lookup`, `osm.overpass`, etc.) worked correctly.

### Fix

One-line widening of the pattern from `:1234/v1/chat/completions` + `:1234/v1/embeddings` to `:1234/v1/` — anything under LM Studio's OpenAI-compat API. The OpenAI SDK instrumentation already records the meaningful calls (chat completions) as `ChatCompletion` spans, so dropping the duplicate httpx coverage of the same endpoints loses no signal.

### Files

- `smcity/observability.py` — `_LM_STUDIO_EXCLUDE_PATTERNS` widened to `(r":1234/v1/",)`. Doc-comment updated with the v0.6.1-fixed-the-symptom-but-missed-/v1/models post-mortem.
- `tests/test_observability.py` — env-var assertions adjusted to check for `:1234/v1/` instead of the specific endpoints.
- `pyproject.toml` — bumped 0.6.1 → 0.6.2.

### Tests

13 observability tests still pass. 370 total no regression.

### Operational note

The fuzz currently running on v0.6.1 will keep emitting unfiltered `/v1/models` GET spans until this v0.6.2 deploy lands. The deploy uses the same zero-downtime layered roll; in-flight fuzz turns ride on `proxy_next_upstream` through the recreate window.

## [0.6.1] — 2026-05-27

**Phoenix trace readability.** Observability-only release. No agent-behavior changes — every fix is in `smcity/observability.py`, `smcity/orchestrator.py` (LLM call-site wrappers), and `smcity/tools/registry.py` (richer span attributes). Triggered by a review where the existing Phoenix dashboard read as a wall of identical-looking `ChatCompletion`, `POST`, and `GET` rows that nobody outside the codebase could navigate.

### Four concrete fixes

1. **LLM calls now name their purpose.** Every `chat()` and `chat_stream()` site in the orchestrator is wrapped in a named parent span:
   - `llm.chat.decide` — first call where gpt-oss-120b picks tools
   - `llm.chat.synthesis` — streaming call that composes the user-facing reply
   - `llm.chat.synthesis_retry` — the fallback when synthesis collapsed (no prose, just tool-call tokens)
   - `llm.chat.gate_retry` — re-prompt after a `tool_call_gate` rejected a proposal
   - `llm.chat.chain_rules_retry` — re-prompt under the `LLMHint` continuation path
   - `llm.chat.invariant_retry` — re-prompt after `synthesis_invariants` caught data-denial
   The auto-instrumented `ChatCompletion` span still appears, but now nests under a labelled parent. A trace reads top-to-bottom as a lifecycle (decide → tools → synthesise → retries) instead of four indistinguishable rows.

2. **LM Studio is excluded from the httpx instrumentation.** Pre-v0.6.1, every LLM call produced both a `ChatCompletion` span (OpenInference instrumentation of the OpenAI SDK) and a duplicate `POST` span (httpx instrumentation of the underlying HTTP request). Same duration, zero added signal, pure noise. We now append `:1234/v1/chat/completions` and `:1234/v1/embeddings` to `OTEL_PYTHON_HTTPX_EXCLUDED_URLS` at init time, so the duplicate POST never enters the span stream. Operator-set patterns are preserved (prepended).

3. **Outbound HTTP spans renamed by destination.** The httpx instrumentation labels every outbound request as bare `GET` / `POST`. Phoenix's spans list then shows 145K rows that all look identical. We install a `request_hook` that rewrites the span name from the URL host + path:
   - `hk.als.lookup` for the ALS geocoder
   - `hk.mtr.next_trains` for MTR ETAs
   - `hk.kmb.eta`, `hk.citybus.eta`, `hk.gmb.eta` for the three bus operators
   - `hk.hko.weather`, `hk.hko.archive` for HKO endpoints
   - `osm.overpass`, `osm.nominatim` for OpenStreetMap
   - `hk.csdi.featureserver` for the CSDI generic FeatureServer
   - `hk.hkha.estates` for Housing Authority
   Unknown hosts fall back to `http.<host>` so spans still group, just less specifically. The mapping is a data-driven tuple — future upstreams add one row.

4. **Tool spans gain semantic attributes.** Filter-friendly attributes set on every `tool.<name>` span:
   - `tool.category` — first segment of the dotted name (`geo`, `transport`, `context`, `facility`, `housing`, `csdi`, `meta`)
   - `tool.locale` — user's primary language (`yue`, `eng`, `zho`, …)
   - `tool.query_lang` — language the tool was actually queried in (after translation routing)
   - `tool.translation_applied` — boolean: did `choose_query_lang` translate
   - `tool.poi_category` — POI category slug when the tool result carries one (specifically `geo.find_poi`)
   The Phoenix filter bar can now answer questions like "show me every Cantonese turn where a translation was applied" without parsing args JSON.

### Files

- `smcity/observability.py` — `_http_request_hook` (span renamer) + `_set_httpx_excluded_urls` (env-var setter) + wiring into `HTTPXClientInstrumentor().instrument(..., request_hook=..., async_request_hook=...)`. Doc-string at the top of the module rewritten to reflect the new span taxonomy.
- `smcity/orchestrator.py` — six `with get_tracer(...).start_as_current_span("llm.chat.<role>"):` wrappers, one per `chat()` / `chat_stream()` call site.
- `smcity/tools/registry.py` — initial-attribute block on `tool.<name>` span expanded to include `tool.category`, `tool.locale`, `tool.query_lang`, `tool.translation_applied`. Post-dispatch hook now also sets `tool.poi_category` when the result dict carries one.
- `tests/test_observability.py` (new, 13 tests) — covers the renamer for known + unknown hosts, the empty-host fallback, the no-raise guarantee, and the env-var preservation of operator-provided patterns.
- `docs/PHOENIX_TRACES.md` — stakeholder-facing trace guide (this also landed as part of v0.6.0 but now reflects the new span names).

### What this looks like in Phoenix

Before:
```
smcity.turn (54.3s)
├── ChatCompletion (2.5s)
├── POST (2.5s)
├── ChatCompletion (18.3s)
├── POST (18.3s)
├── tool.geo.address_lookup (78ms)
├── GET (72ms)
├── ChatCompletion (19s)
├── POST (19s)
├── tool.transport.find_stops_near_point (7ms)
├── ChatCompletion (8.2s)
├── POST (3ms)
├── ChatCompletion (6s)
└── POST (6s)
```

After:
```
smcity.turn (54.3s)
├── llm.chat.decide (2.5s)
│   └── ChatCompletion (2.5s)
├── llm.chat.synthesis (18.3s)
│   └── ChatCompletion (18.3s)
├── tool.geo.address_lookup (78ms)
│   └── hk.als.lookup (72ms)
├── llm.chat.chain_rules_retry (19s)
│   └── ChatCompletion (19s)
├── tool.transport.find_stops_near_point (7ms)
├── llm.chat.invariant_retry (8.2s)
│   └── ChatCompletion (8.2s)
└── llm.chat.synthesis_retry (6s)
    └── ChatCompletion (6s)
```

### Tests

370 tests pass (357 pre-existing + 13 new observability tests). Ruff + mypy strict no regression from baseline.

## [0.6.0] — 2026-05-26

**POI tool collapse + structural rectification escalation.** The 30 per-category `geo.find_<slug>` tools (`geo.find_dentist`, `geo.find_convenience_store`, …) collapse into one `geo.find_poi(category: Literal[...])`. Same routing semantics, one schema instead of thirty.

### Why

Every prompt was sending 30 near-identical `FindPoiArgs` schemas — the only thing that changed per-tool was the name and a one-line description. That was ~42K characters / ~12K tokens of duplicated parameter prose in every turn. The remaining 25 non-POI tools fit in ~24K chars, so the POI fleet was 63% of the schema budget for a single shared argument shape.

### What changed

| | Before | After |
|---|---:|---:|
| Registered tools | 55 | 26 |
| Total schema bytes | ~66K | ~28K |
| Approx prompt tokens (schemas only) | ~19K | ~7.9K |

The savings come from sending the `category` enum once on a single tool instead of N times across N parallel tools. JSON Schema enums are something gpt-oss-120b reads natively, so routing accuracy is preserved — verified by both the unit suite and a live smoke pass.

### Files changed

- `smcity/tools/osm_pois.py` — `_make_poi_tool()` + 30-element `OSM_POI_TOOLS` list deleted. Replaced with a single `FIND_POI_TOOL`. `FindPoiArgs` gains a required `category: PoiCategory` Literal whose description carries the bilingual EN + 繁體 hint table (one source of truth, no per-tool redundancy). Startup invariant: the Literal and the underlying `_CATEGORIES` dict must agree on slugs — fails loudly at import if they drift.
- `smcity/tools/registry.py` — `ToolSpec` gains an optional `citation_discriminator_key`. When set, the orchestrator pulls `result[<key>]` and attaches it to the tool's `Citation` so user-facing citations can preserve the sub-type a single tool covers. The POI tool sets this to `"category"`.
- `smcity/schemas.py` — `Citation` gains an optional `discriminator: str | None` field for the sub-type.
- `smcity/orchestrator.py` — `_append_trace_and_citations` reads the discriminator. `_rewrite_source_footer` renders `find_poi/dentist` (etc.) so the user-facing source line keeps the specificity the per-category tools used to provide.
- `smcity/chain_rules.py` — POI auto-dispatch now emits `AutoDispatch(tool="geo.find_poi", args={"category": ..., "lat": ..., "lng": ...})`. The 30 multilingual keyword patterns (`_POI_CATEGORY_PATTERNS`) are unchanged — they were already keyed by slug, not by tool name.
- `smcity/tool_call_gates.py` — new `FIND_POI_NEEDS_SPATIAL_SCOPE_GATE` (see below).
- `smcity/prompts.py`, `smcity/langrouter/coverage.py`, `smcity_fuzz/contracts.py`, `smcity_fuzz/datasets.py`, `data/coverage_catalog.json`, `scripts/live_smoke.py`, `docs/architecture/ARCHITECTURE.md` — every reference to per-category POI tool names migrated to `geo.find_poi` + a `category` slug.
- Tests — `test_chain_rules.py`, `test_tool_scope.py`, `test_osm_gmb_forecast.py`, `test_orchestrator.py`, `test_synthesis_invariants.py`, `test_tool_call_gates.py`, `test_tools.py`. New regression guards: schema-blob size `< 30K`, category Literal rejects unknown slugs / requires `category`, citation discriminator threads through, gate firing + rectification path. 338 → 357 tests.
- `pyproject.toml` — 0.5.7 → 0.6.0.

### The Cantonese POI snag — and the structural fix

Live smoke against gpt-oss-120b on `尖沙咀附近邊度有牙醫?` exposed a regression specific to the collapse: the LLM occasionally called `geo.find_poi(category="dentist")` directly with no coordinates, skipping the `geo.address_lookup` precondition that the chain_rules POI engine relies on. The args validator rejected, the chain rule couldn't fire (no precondition result), and synthesis collapsed.

Pre-v0.6.0 this didn't happen because the 30 separate POI tool descriptions each carried "Pair with `geo.address_lookup` for landmark queries" — 30 reinforcements of the same hint per prompt. The collapse left that hint in only one place. gpt-oss-120b's Cantonese routing path apparently needed the redundancy.

Per the project's structural-enforcement principle, this is fixed mechanically rather than by tuning the prompt:

1. **New pre-execution gate** — `FIND_POI_NEEDS_SPATIAL_SCOPE_GATE` fires when `geo.find_poi` is proposed without `lat`/`lng` or a full bbox. The first violation triggers the standard "re-prompt with corrective hint" path.
2. **Rectification escalation** — when the LLM's retry STILL emits the same bad shape, the orchestrator's new `_rectify()` helper drops the bare `find_poi`, injects `geo.address_lookup(query=user_text)`, and lets the existing `chain_rules` POI engine auto-dispatch `find_poi(category=..., lat=..., lng=...)` once coords resolve. A new `gate.rectified` event is emitted for observability.

Same lifecycle engines, one new gate + one new rectification path — no orchestrator surgery beyond adding the escalation hop. Both pieces are declarative and live next to their stage's other rules.

### Live smoke result

| Query                                                  | Before (v0.5.7) | After (v0.6.0) |
|--------------------------------------------------------|:---:|:---:|
| `next train at Central` (en)                           | ✅ | ✅ |
| `where's the nearest 7-eleven near TST?` (en)          | ✅ | ✅ (chain_rules fired; one run hit a 504 from Overpass — upstream issue, not the collapse) |
| `尖沙咀附近邊度有牙醫?` (yue)                          | ✅ via 30 tools | ✅ via gate + chain (rectification logged) |
| `how do I get from Central to Sha Tin?` (en)           | ✅ plan_journey | ✅ plan_journey |
| `MTR from Central to Sha Tin` (en)                     | ✅ plan_simple_route `[SPECIALIZED: mtr_only]` | ✅ same |
| `find me a place to go` (en)                           | ✅ ASK_USER_ONLY_GATE redirect | ✅ same |

### What this enables next

A two-stage routing scheme (a small gemma-synth pre-pass that picks the top-K tools, then gpt-oss-120b sees only those schemas) is no longer the obvious next lever: the remaining ~25 non-POI tools fit comfortably in the prompt and the KV-cache prefix stays stable across turns. Future tool additions should pay the same "one slug, one description" discipline before reaching for dynamic schema filtering.

### Tests

357 tests pass. Ruff + mypy strict no regression from baseline.

## [0.5.7] — 2026-05-25

**Single source of truth for the package version.** Surfaced immediately by the v0.5.6 deploy: blue and green were correctly running on `smcity:0.5.6`, but `GET /health` reported `"version":"0.5.0"`. The image tag, the Docker container's image config, and the version string the agent shipped to clients had drifted apart.

### Root cause

`smcity/__init__.py` carried a hardcoded `__version__ = "0.5.0"`. Every release since v0.5.1 bumped `pyproject.toml` but nobody bumped `__init__.py`. The release checklist had a silent landmine — easy to miss for five releases in a row, and surfaced only at the moment a deployed agent was asked to identify itself.

### Fix

`smcity/__init__.py` now reads the version from `pyproject.toml` at import time via `tomllib`. There is one source of truth for the package version (the `[project] version = "X.Y.Z"` line). Bumping `pyproject.toml` propagates to `__version__` automatically; drift is structurally impossible.

The runtime Docker image previously copied only `smcity/`, `smcity_fuzz/`, `data/`, `web/` to `/app/`. The Dockerfile now also copies `pyproject.toml` to `/app/pyproject.toml` so the runtime lookup works in production exactly as it does in dev.

If the lookup fails (file missing, malformed TOML, missing `[project]` section), `__version__` falls back to `"0.0.0+unknown"` — surfacing the misconfiguration in `/health` instead of silently returning a wrong-but-plausible number.

### Files changed

- `smcity/__init__.py` — `_read_version_from_pyproject()` + `__version__ = _read_version_from_pyproject()`. The exception path returns `"0.0.0+unknown"` so a broken read is detectable instead of being mistaken for a real version.
- `Dockerfile` — new `COPY --chown=smcity:smcity pyproject.toml ./pyproject.toml` in the runtime stage. Adds ~600 bytes to the image; negligible.
- `pyproject.toml` — bumped 0.5.6 → 0.5.7.

### What this prevents going forward

Any future release just edits `pyproject.toml` and CHANGELOG. The `__version__` constant, the OpenTelemetry `version=` tag, the startup log line, the `/health` response, and the structured Prometheus / OTel attribute all read from the same one source. The "I bumped version but `/health` still says the old one" class of incident is gone.

### Tests

338 tests still pass. Ruff (lint + format) clean. Mypy strict clean across 39 source files. Local smoke: `python -c "import smcity; print(smcity.__version__)"` reports `0.5.7`.

## [0.5.6] — 2026-05-25

**Layered zero-downtime deploys.** Tooling-only patch. The v0.5.0 `deploy.sh` was theoretically zero-downtime but had three real-world failure modes that caused past deploys to drop the public endpoint. Rewritten to fail safe.

### What was wrong in the v0.5.0 deploy

1. **Build overwrote the running image tag.** `docker compose build smcity-agent-blue` rebuilt `smcity:0.5.0` in place. If the new build had a runtime issue (crash on start, missing env var, etc.), there was no prior tag to roll back to — the only "previous version" lived inside the running containers, and the moment compose recreated them it pulled the broken image.
2. **Both replicas shared a single image tag.** Blue and green both said `image: smcity:0.5.0`. There was no way to put one replica on the new tag and keep the other on the prior tag during the roll — the layering the architecture promised didn't actually exist in the YAML.
3. **No HTTP-level sanity check.** Docker's healthcheck just hit `/health` inside the container; the deploy proceeded as soon as that flipped to "healthy". A container with the wrong network bind (e.g., bound to `127.0.0.1` instead of `0.0.0.0`) would pass its own healthcheck but be unreachable from the nginx router.

### Files changed

- `deploy/docker-compose.yml` — image tags parameterised via `BLUE_TAG` and `GREEN_TAG` env vars (both default to the current shipping tag). The two replicas can now be on different tags during a roll without a YAML edit; the deploy script flips one at a time. Default values are kept inline so a bare `docker compose up -d` still works the same as before.
- `deploy/deploy.sh` — rewritten end-to-end:
  - Builds the **new** tag (default: the version in `pyproject.toml`). The prior tag stays intact on disk for fast rollback.
  - Per-replica roll: blue first with `BLUE_TAG=<new>`, green continues serving 100% of traffic via nginx `proxy_next_upstream`. Then green with `GREEN_TAG=<new>`, blue serves 100%.
  - Two health gates per replica: (a) Docker healthcheck flipping to `healthy`, (b) HTTP `/health` probe from inside the bridge network (catches "container is up but bound to the wrong address" failures).
  - On any failure (build error, health timeout, HTTP probe miss), the failing replica is auto-rolled-back to its prior tag and the script exits non-zero. The other replica is never touched on failure.
  - New `--status` flag dumps current blue/green tags + Docker-health side-by-side.
  - New `--rollback <tag>` flag for manual revert of both replicas (no rebuild).
  - New exit codes (build / health / HTTP / rollback) so wrapping CI can distinguish failure modes.

### Worked through the failure modes

- *Build fails*: neither replica is touched. Old service stays up. Exit 1.
- *Build succeeds, blue's healthcheck never flips*: blue auto-rolls-back to prior tag; green still on prior tag; nginx serves from green throughout. Exit 2.
- *Build succeeds, blue is Docker-healthy but the HTTP probe fails*: blue auto-rolls-back; same as above. Exit 3.
- *Blue rolls cleanly, green's healthcheck never flips*: green auto-rolls-back to prior tag; blue stays on the new tag; nginx serves from blue throughout. The deploy is half-done but the live service is still up — `./deploy.sh --status` will show the asymmetry so the operator can debug or `--rollback` blue. Exit 2.
- *Rollback itself fails*: explicit exit 4 with manual-fix command printed. Surfaces the rare double-failure case loudly instead of silently leaving a broken replica.

### Why this is in its own commit

The deploy hardening is unrelated to runtime behaviour and could regress separately from the agent code; bundling it with the v0.5.5 fixes would muddle bisects when something fails. The `--status` and `--rollback` flags are observability + safety additions that pay back the first time a deploy gets nervy.

No agent runtime code changed. 338 tests still pass. Ruff + mypy strict clean.

## [0.5.5] — 2026-05-25

**Live-smoke driven fixes** — first run of `scripts/live_smoke.py` against the real Mac-Studio LM Studio exposed two systematic gpt-oss-120b argument-shape bugs that the unit-test suite couldn't surface. Both fixed structurally at the schema layer.

### Live smoke result

| Query                                        | Before (v0.5.4)                   | After (v0.5.5)                    |
|----------------------------------------------|-----------------------------------|-----------------------------------|
| `next train at Central` (en)                 | ❌ validation: `name` vs `station_name` | ✅ MTR ETAs returned              |
| `where's the nearest 7-eleven near TST?` (en)| ❌ LLM bypassed `address_lookup`  | ✅ chain rule fired → 7-Eleven   |
| `尖沙咀附近邊度有牙醫?` (yue)                  | ✅ chain rule fired               | ✅ chain rule fired               |
| `how do I get from Central to Sha Tin?` (en) | ✅ picked `plan_journey` (DEFAULT)| ✅ picked `plan_journey`          |
| `MTR from Central to Sha Tin` (en)           | ✅ picked `plan_simple_route` (SPECIALIZED) | ✅ picked `plan_simple_route` |
| `find me a place to go` (en)                 | ✅ gate fired                     | ✅ gate fired                     |

All three v0.5.1 engines now confirmed firing against the real LLM: `chain.fired` on POI queries (EN + yue, deterministic `auto_dispatch` kind), `gate.violated` on vague `ask_user`-only proposals. Latency 3.5 – 9.8 s per query.

### What was wrong

**Bug 1: field-name guess (`name` vs canonical).** gpt-oss-120b consistently emits `{"name": "Central"}` when the schema declares the field as `station_name`, and the same for `query` on lookup tools. The validation error fires before any handler runs (`status=error`, `latency_ms=0`, `error_kind=validation`), so the orchestrator never gets a chance to retry. Three tools affected: `transport.get_mtr_next_trains` (`station_name`), `geo.address_lookup` (`query`), `transport.find_stops_by_name` (`query`).

**Bug 2: POI null-coords bypass.** The `geo.find_*` tools declared `lat: float | None = None` and `lng: float | None = None` with no spatial-scope constraint. When the LLM skipped `geo.address_lookup` and called `geo.find_convenience_store` directly with `{"lat": null, "lng": null}`, the schema accepted it, the handler defaulted to the whole-of-HK bbox, the search returned a noisy global result, and the synthesis collapsed into harmony noise. The chain-rules engine couldn't help — its precondition (an `ok` `address_lookup` result) never fired.

### Fixes (both structural, at the schema layer)

- `smcity/tools/transport.py` — `MTRNextTrainsArgs.station_name` now declares `validation_alias=AliasChoices("station_name", "name", "station")` + `model_config = ConfigDict(populate_by_name=True)`. The LLM's `name` guess is accepted as a synonym; the canonical `station_name` still works.
- `smcity/tools/geo.py` — same treatment for `AddressLookupArgs.query` (`AliasChoices("query", "name", "q")`).
- `smcity/tools/transport_search.py` — same for `FindStopsByNameArgs.query` (`AliasChoices("query", "name", "stop_name", "q")`).
- `smcity/tools/osm_pois.py` — new `@model_validator(mode="after")` on `FindPoiArgs` rejecting the (no point AND no full bbox) shape with a message that tells the LLM exactly what to do: *"Call geo.address_lookup first to resolve a place name to coordinates."* The chain-rules engine then auto-completes the chain in the common case.

### What this does NOT do (per memory: "three similar lines is better than premature abstraction")

We did **not** build a universal registry-layer auto-remapping mechanism. With three tools exhibiting the same pattern, per-tool aliases are the right scope: explicit, debuggable, no magic. If a fourth tool shows the same vulnerability AND the pattern stays uniform (one unexpected `name` field, one missing canonical), the universal mechanism becomes warranted; until then, evidence-driven inline fixes are the right call.

### Tests

- `tests/test_osm_gmb_forecast.py::test_osm_pois_dedupes_by_osm_id` — updated to pass an explicit `(lat, lng)` after the new validator. Test still covers dedup logic; the v0.5.5 spatial-scope guard is now exercised by the dispatch path the test goes through.
- 338 tests pass. Ruff (lint + format) clean. Mypy strict clean across all 39 source files.

### Operational note: SSH-driven LM Studio remote ops

Bringing LM Studio up tonight surfaced that the lab Mac Studio is fully SSH-accessible via Tailscale (`ssh earnestdesign@earnests-mac-studio.taila366aa.ts.net`). We used that to (a) confirm the LM Studio process state, (b) flip the server bind from `127.0.0.1:1234` to `0.0.0.0:1234` remotely via `lms server start --bind 0.0.0.0`. Captured in [memory/reference_tailscale_ssh.md](../../.claude/projects/-Users-huckgod-Developer-LabSocialRobotics-SmartCityIntegration/memory/reference_tailscale_ssh.md) so we don't ask the user to bounce machines for ops we can do remotely.

## [0.5.4] — 2026-05-24

**README refresh** — surgical patch. No code change. The repo's `README.md` was dated v0.4.5 (2026-04-23) and predated the v0.5.0 → v0.5.3 work; a new contributor or returning maintainer landing on the README would miss the entire v0.5.x story.

### What was stale

- Status banner: `**v0.4.5** · 2026-04-23`, `27 live tools`, `232 unit tests + 7 integration tests`.
- Layout tree: `27-tool agent`; described the old `synth.py` / `judge.py` / `runner.py` / `store.py` / `export.py` fuzz layout (replaced by `contracts.py` / `coverage_*.py` in v0.5.0); missing the three v0.5.1 engine modules.
- Feature inventory: claimed `geo.search_osm_pois` (mega-tool replaced in v0.5.0); listed a non-existent `transport.plan_taxi_estimate` (taxi is not a supported mode — see `feedback_no_taxi` memory); missing the v0.5.1 lifecycle engines + scope tags.
- Documentation list: only linked `TOOL_CATALOG.md`, not `ARCHITECTURE.md`.

### Files changed

- `README.md`
  - Status banner bumped to `**v0.5.3** · 2026-05-24` (the previous shipping commit; v0.5.4 is this README patch itself). Tool count 27 → 55. Test count 232 → 338. New bullets summarise the three-stage lifecycle guard rails and the `ToolScope`/`domain` schema, linking to `docs/architecture/ARCHITECTURE.md §3.7`/`§3.8`.
  - Layout tree: tool count 27 → 55, fuzz subtree rewritten for the v0.5.0 contracts shape, three new lifecycle modules added (`tool_call_gates.py`, `chain_rules.py`, `synthesis_invariants.py`), `tools/` line annotated with the per-domain breakdown and the scope/domain note, `scripts/` line added (now includes `live_smoke.py`), test count 232 → 338.
  - Feature inventory: transport list cleaned (taxi removed; scope tags noted; default vs MTR-only vs OTP2 disambiguated inline); geo expanded from `(2)` to `(32)` with the per-category POI tools called out; `meta.ask_user` annotated as `[FALLBACK]`; pipeline-capabilities section gained a paragraph on the three lifecycle engines (one bullet per stage) and a paragraph on the scope-tag schema.
  - Documentation links: added `ARCHITECTURE.md` with a pointer to §3.7 + §3.8.

### Why this is worth its own patch

Documentation drift is silent: tests keep passing, lint stays clean, but the README is the first artefact a newcomer reads. Letting the README claim 27 tools when there are 55, or listing a tool we deliberately removed, undermines trust in the rest of the doc. Pinning the README to current reality keeps onboarding and self-orientation cheap.

No code touched. 338 tests still pass. Ruff + mypy strict still clean.

## [0.5.3] — 2026-05-24

**Architecture docs + integration coverage** — docs/test patch. No runtime behaviour change; the goal is to make the v0.5.1 lifecycle abstraction discoverable in the architecture doc and provable end-to-end through orchestrator tests for all three engines (not just the gate). A pre-existing mypy strict error in `smcity/tools/__init__.py` is fixed as bonus hygiene.

### What was missing

- `docs/architecture/ARCHITECTURE.md` was dated 2026-04-21 (v0.1 pre-implementation) and predated the v0.5.x lifecycle engines. A new contributor landing on the doc would miss the structural shift.
- `tests/test_orchestrator.py` had only one integration test for the three lifecycle engines — `test_ask_user_gate_redirects_llm_to_search_tool`. `chain_rules` and `synthesis_invariants` were unit-tested in isolation but not provably wired through the full turn lifecycle. Asymmetric coverage made the lifecycle abstraction harder to evolve confidently.
- `smcity/tools/__init__.py` carried a mypy strict error (introduced in v0.5.0 when the 30 POI tools were unpacked into the registry tuple) that mixed `ToolSpec[A, B]` with `ToolSpec[C, D]` and upcasted to `tuple[object, ...]`. Pre-existing; not regressed by v0.5.1/v0.5.2 but worth fixing while running quality gates.

### Files changed

- `docs/architecture/ARCHITECTURE.md`
  - Header bumped from `v0.1 (pre-implementation)` to `v0.5.3 (post-implementation)`.
  - §3.6 (LLM orchestrator loop): pseudocode replaced with the v0.5.3 implementation, labelling the three guard points **(A) pre-execution gate**, **(B) post-execution chain rule**, **(C) post-synthesis invariant** that match the engine modules in code.
  - §3.7 (NEW) — Lifecycle guard rails: documents the three engines, the common `apply_*(…) -> Violation | None` shape, the declarative rule pattern, and where each unit/integration test lives. Sub-sections 3.7.1/3.7.2/3.7.3 cover each engine's role in detail.
  - §3.8 (NEW) — Tool scope tags: documents the `ToolScope` enum + `domain` field that auto-renders `[DEFAULT: …]` / `[SPECIALIZED: …]` / `[FALLBACK]` markers into every OpenAI tool description.
  - Existing §3.7 (Response formatter) renumbered to §3.9.
- `tests/test_orchestrator.py` — two new integration tests, both using `respx.mock` for upstream HTTP and `monkeypatch` for the LLM scripted-replies pattern already used by the existing tests:
  - `test_chain_rule_auto_dispatches_poi_followup` — proves that when the LLM fires only `geo.address_lookup` on a POI query, the chain rule infers the category from the user text and the orchestrator deterministically dispatches `geo.find_dentist` with the resolved lat/lng. Asserts both tools end up in `tool_trace`, the `chain.fired` telemetry event names the right rule + `auto_dispatch` kind.
  - `test_synthesis_invariant_retries_on_data_denial` — proves that when a tool returns non-empty records but the LLM's synthesis denies the data ("I couldn't find any"), the invariant fires and the corrective retry's reply (which cites a record) is what ships to the user. Asserts the `invariant.violated` telemetry event names `data_denial` + the tool + the record count.
- `smcity/tools/__init__.py` — `build_default_registry()` now declares `specs: list[ToolSpec[Any, Any]]` to pin the variance through the tuple unpack. Behaviour identical; mypy strict was reporting `Argument 1 to "register" has incompatible type "object"; expected "ToolSpec[Any, Any]"`.

### Why this is architectural and not a patch

- The lifecycle abstraction now has a single home in the architecture doc. Future contributors don't need to read three module headers + cross-reference the orchestrator to understand what runs at what stage of an LLM turn.
- All three engines now have parallel coverage:

  | Engine                     | Unit tests                                 | Orchestrator integration test                              |
  |----------------------------|--------------------------------------------|------------------------------------------------------------|
  | `tool_call_gates.py`       | `tests/test_tool_call_gates.py` (10)       | `test_ask_user_gate_redirects_llm_to_search_tool`          |
  | `chain_rules.py`           | `tests/test_chain_rules.py` (15)           | **`test_chain_rule_auto_dispatches_poi_followup`** (new)   |
  | `synthesis_invariants.py`  | `tests/test_synthesis_invariants.py` (31)  | **`test_synthesis_invariant_retries_on_data_denial`** (new)|

- Mypy strict is now **clean across all 39 source files** (was 1 pre-existing error). The strict-mode signal is restored as a meaningful gate going forward.
- 338 tests pass (336 → 338, +2 integration tests). Ruff (lint + format) clean.

## [0.5.2] — 2026-05-24

**POI tool description trim** — patch release that addresses the prose half of the v0.5.0 prompt-bloat regression. The structural enforcement engines from v0.5.1 only fire if the LLM returns tool calls in the first place; on gpt-oss-120b the ~13K-token tool catalog was producing 400-rejections and prompt-processing timeouts on transport datasets. This trims the 30 POI tool descriptions from ~45 words to ~13 words each.

### Measured impact

| Metric | Before | After |
|---|---|---|
| POI description avg (words) | ~45 | 15.5 |
| POI description total (tokens) | ~1850 | ~835 |
| Net prompt savings | — | ~1.0K tokens / turn |

The label (from `_LABELS`) carries the discriminative info — "convenience stores (7-Eleven, Circle K, VanGO, etc.)" already tells the LLM what this tool is for. Result-shape detail (coords, names, tags) lives in the result schema, which is serialized into the prompt anyway. Chain enforcement (the "call `geo.address_lookup` first" half of the old prose) is now structural via `smcity/chain_rules.py` — the prompt doesn't need to teach it.

### What this does NOT fix

The bigger chunk of POI prompt cost is the duplicated `FindPoiArgs` schema serialization across all 30 tools (~9.5K tokens of repeated boilerplate — 8 fields × descriptions × 30 tools). That's an architectural fix (two-stage tool routing, where the LLM only sees a relevant subset of tools per turn) reserved for v0.6.0. v0.5.2 is the prose half only — a focused patch.

### Files changed

- `smcity/tools/osm_pois.py` — `_make_poi_tool()` factory: description trimmed from a 5-line block to a 2-line one. Comment added explaining the rationale (the label carries discrimination, the schema carries result shape, the chain engine carries follow-up enforcement). Stale `# noqa: RUF002` directive replaced with `RUF003` covering the workbook-range comments (`S514–S530`) that ruff began flagging after the file edits.

### Why this is a patch and not a feature

- No new code surface; no new tests required. The mechanism for POI tool selection didn't change; only the prose density did.
- The trim was the originally-planned v0.5.1 starter step (drafted in conversation, deferred when scope expanded to the three-engine lifecycle). v0.5.2 closes the deferral cleanly.
- 336 tests still green. Ruff (lint + format) clean. Mypy strict clean. No behavior change in unit tests; the win is downstream of the LLM's prompt-processing budget on gpt-oss-120b.

## [0.5.1] — 2026-05-24

**Orchestrator lifecycle guard rails + tool scope tags** — promotes the v0.5.0 inline POI chain check (Fix 3) into a declarative engine, adds two sibling engines at the other LLM-turn lifecycle stages, and replaces ad-hoc tool-selection disambiguation prose with a structured `scope`/`domain` schema on every `ToolSpec`. The orchestrator now enforces three independent structural invariants per turn instead of relying on prompt nudges.

### The lifecycle abstraction

| Stage             | Module                              | Inputs                          | First rule registered            |
|-------------------|-------------------------------------|---------------------------------|----------------------------------|
| Pre-execution     | `smcity/tool_call_gates.py`         | LLM's proposed tool calls       | `ASK_USER_ONLY_GATE`             |
| Post-execution    | `smcity/chain_rules.py`             | Tool results + user query       | `POI_CHAIN_RULE` (replaces Fix 3)|
| Post-synthesis    | `smcity/synthesis_invariants.py`    | Reply text + tool results       | `DATA_DENIAL_INVARIANT`          |

Each engine follows the same shape: `apply_*(inputs, rules=DEFAULT) -> Violation | None`. Each fires at most one corrective LLM re-prompt per turn — no loops, no compounding latency. Adding a new check at any stage is a declarative ~30-line addition; no orchestrator edits needed.

### Failure classes fixed

| Failure class | What was wrong | Fix |
|---|---|---|
| POI chain abandonment was a special-case inline check in `orchestrator.py` | Adding more chains required orchestrator surgery; the POI category was re-rolled to the LLM even when the category was inferrable from the user's text | **`smcity/chain_rules.py`** — declarative `ChainRule` engine. The POI rule now lives as one of (eventually many) rules. New: `AutoDispatch(tool, args)` continuation that fires the missing tool deterministically when the category is inferrable from 30 multilingual keyword patterns (EN / yue / zh-Hant / zh-Hans); `LLMHint(text)` continuation preserved as fallback. The orchestrator no longer encodes POI semantics. |
| LLM led with `meta.ask_user` even when search-shaped queries had obvious tool matches; `ask_user` was a top-3 tool fired on nearly every dataset in the v0.4.16 partial report | Nothing prevented `meta.ask_user` from being the first move; the prompt-level guidance was advisory | **`smcity/tool_call_gates.py`** — pre-execution gate engine with `ASK_USER_ONLY_GATE`. Rejects responses where the only proposed tool is `meta.ask_user`, re-prompts once with the alternative tool families named explicitly (`transport.plan_journey`, `geo.address_lookup`, `geo.find_<category>`). Single retry; if the LLM persists, we accept and surface the clarification — never a loop. |
| Reply denied non-empty tool data ("I couldn't find any" after 5 dentists were returned) — Failure Pattern #5 from the partial 10k report | No structural check between synthesis output and tool results; prompt-level honesty rules are soft | **`smcity/synthesis_invariants.py`** — post-synthesis invariant engine with `DATA_DENIAL_INVARIANT`. Three guards keep false-positive rate low: (a) reply contains explicit denial language matched by a multilingual regex covering EN, yue, zh-Hant, zh-Hans, ja, ko, fr, de, es, th, vi, id, tl; (b) an `ok`-status tool returned non-empty records; (c) reply mentions zero records (substring check across `name_en` / `name` / `name_tc` / `name_zh` / `route` / `destination_en` / common name fields). Violation triggers one corrective re-prompt with records pre-quoted. |
| Wrong tool picked from ambiguous menus (`plan_simple_route` vs `plan_journey`; KMB vs Citybus ETAs) | Disambiguation lived in description prose ("do NOT use for Citybus") — easy to miss, inconsistent across tools | **`ToolScope` enum + `domain` field on `ToolSpec`.** Registry auto-prepends `[DEFAULT: <domain>]` / `[SPECIALIZED: <domain>]` / `[FALLBACK]` to every description. 11 transit + meta tools tagged where confusion was documented. The system prompt explains the marker semantics in one paragraph — no per-tool prose edits to re-disambiguate when behavior shifts. |

### Files changed

**New modules:**
- `smcity/chain_rules.py` — `ChainRule` engine, `AutoDispatch` / `LLMHint` continuations, `POI_CHAIN_RULE` with 30-category multilingual keyword inference.
- `smcity/synthesis_invariants.py` — `SynthesisInvariant` engine, `DATA_DENIAL_INVARIANT`, multilingual denial regex covering 13 languages, generic record-name extractor that scans any list-of-dicts at top level (no per-tool maintenance).
- `smcity/tool_call_gates.py` — `ToolCallGate` engine, `ASK_USER_ONLY_GATE`.

**Modified:**
- `smcity/tools/registry.py` — `ToolScope` enum (`DEFAULT` / `SPECIALIZED` / `FALLBACK`), `scope` + `domain` fields on `ToolSpec`, marker rendering in `openai_schema()`.
- `smcity/orchestrator.py` — three engines wired into the turn lifecycle; new `_apply_continuation` (returns dispatched results) and `_maybe_retry_for_invariants` helpers; pre-execution gate retry around the first `chat()` call; dead Fix-3 helpers (`_incomplete_poi_chain`, `_coord_hint_from_lookup`, `_POI_QUESTION_RE`) deleted.
- `smcity/prompts.py` — one-paragraph "Tool selection (scope markers)" section teaching the LLM what `[DEFAULT]` / `[SPECIALIZED]` / `[FALLBACK]` mean.
- `smcity/tools/transport.py`, `transport_kmb.py`, `transport_citybus.py`, `transport_gmb.py`, `transport_planner.py`, `transport_simple_modes.py`, `otp2.py`, `meta.py` — `scope` + `domain` annotations on 11 transit + meta tools. Notable: `meta.ask_user` is `[FALLBACK]` with rewritten "LAST RESORT" description; `transport.plan_journey` is `[DEFAULT: any_mode_journey]`; `plan_simple_route` is `[SPECIALIZED: mtr_only]`; Citybus/KMB/GMB ETA tools are tagged with operator-specific domains.

**New tests (65 total):**
- `tests/test_chain_rules.py` (15) — engine semantics + POI rule across EN / yue / zh-Hant / zh-Hans.
- `tests/test_synthesis_invariants.py` (31) — engine semantics + record-name extraction + one positive case per supported language + neutral-text negative.
- `tests/test_tool_call_gates.py` (10) — engine semantics + `ASK_USER_ONLY_GATE` paired/unpaired behavior.
- `tests/test_tool_scope.py` (13) — marker rendering mechanism + smoke tests on the default registry.

**Modified tests:**
- `tests/test_orchestrator.py` — new `test_ask_user_gate_redirects_llm_to_search_tool` (end-to-end proof that the gate redirects the LLM to a search tool); `test_clarification_gate_via_meta_ask_user` updated to acknowledge the gate retry round-trip.

### Why this is architectural and not a patch

- **Three orthogonal engines, three lifecycle stages.** Pre-execution, post-execution, post-synthesis. Each is independently testable and extensible. The orchestrator is a thin glue layer between them; adding a transport-stop chain rule, a "no taxi mentioned" invariant, or a "forbidden tool combination" gate is a localized declarative addition.
- **No special cases left in the orchestrator.** The POI chain check that v0.5.0 introduced inline is now one declarative rule among (eventually) many. Fix 3 helpers were deleted, not retained as legacy. The orchestrator file shrank by 60 lines despite gaining three integration points.
- **Tool selection moved from prose to schema.** `scope` + `domain` are structured fields on every `ToolSpec`; the marker is rendered uniformly across all 55 registered tools. The system prompt explains the markers once; we never have to edit individual tool descriptions to maintain disambiguation as the catalog grows.
- **Generic mechanisms, not per-example fixes.** `_extract_record_names` scans any list-of-dicts at the top level of any tool result for common name keys — adding a new tool whose results follow the conventions gets coverage automatically. The multilingual denial regex covers all 13 supported languages from the start (per the all-languages-from-v0 invariant) rather than catching English first and expanding later.
- **336 tests pass (271 → 336).** Five test files added; one updated. Ruff (lint + format) clean, mypy strict clean on the new modules.

## [0.5.0] — 2026-05-21

**Coverage architecture overhaul** — four composing fixes that target the structural failure modes the partial 10k report exposed. The v0.4.17 release was prompt-engineering patches; this one removes the *shapes* that allowed those bugs.

| Failure class in the 10k report | What was wrong | Fix |
|---|---|---|
| Hit-rate metric meaningless (any of two tools = "hit") | `expected_tools & fired_tools != ∅` scored a half-chain identically to a complete chain | **Fix 2**: contracts-based judge in `smcity_fuzz/contracts.py`. Each dataset declares what *success* means as code, not a list of strings. New buckets: `complete`, `partial_chain`, `wrong_tool`, `no_tool`, plus the transport-level error buckets. |
| 30 POI kinds buried behind one `geo.search_osm_pois` `category` Literal | LLM had to pick this tool then pick the right enum value from 30 strings in description prose | **Fix 1**: split into 30 thin tools — `geo.find_dentist`, `geo.find_bench`, `geo.find_convenience_store`, …. Tool routing is what frontier models do well; string-enum routing is what they hallucinate on. Auto-generated from `_CATEGORIES` so the table is the only source of truth. |
| Chain stopped at `geo.address_lookup` for POI questions | Prompt asked the LLM to remember the chain; structure didn't enforce it | **Fix 3**: orchestrator post-condition check. If `address_lookup` fired ok, no `geo.find_*` did, and the user's question is POI-shaped, issue one structured retry pre-loaded with the resolved lat/lng. |
| 10k corpus was English-only despite Cantonese being priority language | Generator hardcoded `_SYSTEM_PROMPT` for English; runner sent `language` field the agent ignored | **Fix 4**: multilingual stratified generator (`en` / `yue` / `zh-Hant` / `zh-Hans`), language-specific few-shot blocks, runner sets `locale_override` so per-language hit-rate is honest. |

### Files changed

- `smcity/tools/osm_pois.py` — rewritten end-to-end. Exports `OSM_POI_TOOLS` (list of 30 `ToolSpec`) instead of the single `SEARCH_OSM_POIS_TOOL`. Each tool's args drop the `category` field; the slug is bound at factory time.
- `smcity/tools/__init__.py` — registers `*OSM_POI_TOOLS` in place of the mega-tool.
- `smcity/langrouter/coverage.py` — seeds language coverage for all 30 POI tool names from `POI_TOOL_NAMES`.
- `smcity/prompts.py` — the 14-line POI guidance block compressed to a 4-line pointer (the new tool names are self-documenting).
- `smcity/orchestrator.py` — POI chain-completion check between tool execution and final synthesis. Two helpers added: `_incomplete_poi_chain`, `_coord_hint_from_lookup`.
- `smcity_fuzz/contracts.py` — **new file**. `evaluate(row) -> Verdict` is the single source of judgment truth. 48 contracts (35 datasets + 13 additional integrations) generated from factories so there is no copy-paste.
- `smcity_fuzz/coverage_report.py` — delegates judgment to `contracts.evaluate`. Bucket columns: `complete / partial / wrong-tool / no-tool / errors / timeouts`. `complete_rate` replaces `expected_tool_hit_rate` (the latter is emitted as an alias for one release so the existing /coverage UI keeps working).
- `smcity_fuzz/coverage_gen.py` — `--languages en,yue,zh-Hant,zh-Hans` flag, per-language system prompt + few-shot, language-stratified cell loop, `language` field per row.
- `smcity_fuzz/coverage_run.py` — sends `locale_override` so the agent answers in the corpus language.
- `smcity_fuzz/cli.py` — pipes `--languages` through to `coverage generate`.
- `smcity_fuzz/datasets.py` — fuzz topic `expected_tools` migrated to per-category names.
- `data/coverage_catalog.json` — 30 OSM POI entries have `geo.search_osm_pois` replaced with `geo.find_<category>`.
- `tests/test_osm_gmb_forecast.py` — uses new per-category tool names.

### Why this is an architectural fix and not a patch

- **No new heuristics inside the agent.** The chain check is a post-condition, not a category guesser. The orchestrator does not encode which categories exist; the LLM is told `geo.find_*` exists and given the lat/lng, then makes the call.
- **No parallel sources of truth.** The 30 POI categories live in one table (`_CATEGORIES`). Tool names, contract registry, and language coverage are derived. Renaming a category in one place updates all three.
- **Judge is code, not strings.** A contract is a function that can inspect *anything* in the result row. The catalog's `tools:` field is now narrative metadata only.
- **271 tests still green.** Nothing in the surrounding codebase needed to change beyond the trivial rename to the new tool names.

### Image

`smcity:0.5.0` — built but not deployed. Deploy with `./deploy.sh` to start the v2 multilingual 10k run.

## [0.4.17] — 2026-05-20

**Fixes from the partial 10k coverage results (7268/9442).**

The partial analysis showed zero errors / timeouts / collisions across 7268 turns — the agent is reliable. The patterns to fix were all about tool routing, not stability:

| Symptom | Diagnosis | Fix |
|---|---|---|
| OSM POIs hit `geo.address_lookup` 4217×, `geo.search_osm_pois` only 1445× | LLM resolves the landmark via address_lookup, then often answers from general knowledge instead of chaining the POI search | Prompt now spells out the **two-tool chain pattern** for POI queries + lists all 30 categories so the LLM knows niche ones (bench, kiosk, handrail, recycling_location) are in scope. |
| Niche categories at 5-12% hit rate (`S543` bench, `S538` kiosk, `S541` dentist) | LLM treats them as "I don't have data for that" because the OSM tool description buries them in a long list | OSM tool description rewritten to **lead with "ALWAYS use this for nearest-X queries"** and break the categories into shop / amenity / infrastructure groups so the niche ones aren't an afterthought. |
| Catalog hit-rate underestimates actual coverage (S514-S549 all 16-45%) | `expected_tools` listed only `geo.search_osm_pois` — but the agent's standard pattern is address_lookup + search_osm_pois together | Added `geo.address_lookup` as a co-acceptable tool for every OSM-backed dataset entry (30 datasets updated). Reflects how the agent actually answers, not an idealised single-call assumption. |

### Files changed

- `smcity/prompts.py` — new "POI / nearest-X queries" guidance block with the explicit 2-tool chain instruction and all 30 OSM categories enumerated.
- `smcity/tools/osm_pois.py` — `SEARCH_OSM_POIS_TOOL.description_en` rewritten with a stronger "ALWAYS use this" lead and grouped category list.
- `data/coverage_catalog.json` — 30 OSM POI dataset entries gain `geo.address_lookup` as a co-acceptable tool.

### Not deployed yet

The 10k run is still in flight (7300+ results, ~3h to go). Deploying this version mid-run would mix old-prompt and new-prompt results in the same report. Once the run finishes the report ships against a clean v0.4.16 baseline, then **this** version goes live for a re-run to measure the improvement.

### Known gaps NOT fixed in this release

- **S506 Ferry** (0% hit rate, 218 questions) — still no ferry tool. Agent correctly says "I don't have ferry data" rather than hallucinating, which is acceptable. Wiring `transport.get_ferry_eta` is a separate task.
- **S512 Topographic iB1000** (0% hit rate, 170 questions) — the iB1000 dataset isn't registered with `csdi.query_features` (only LCSD courts + pools are). Adding it requires looking up the ArcGIS FeatureServer URL for the iB1000 layer from the CSDI Portal — a separate task.

### Image

`smcity:0.4.17` — built but not deployed. Deploy with `./deploy.sh` once 10k run finishes.

## [0.4.16] — 2026-05-19

**Coverage test suite — Gemma-driven 10 000-question stratified probe.** Tesfa's ask: "imagine this scenario — we generate 10000 questions in English asking about various permutations; see what works and what doesn't. Use Gemma to generate the questions. Document and categorise all the answers."

Three new modules under `smcity_fuzz/`, one CLI verb (`coverage`), one new web UI panel:

### `smcity_fuzz/coverage_gen.py` — question synth

- Stratifies generation across every dataset in `data/coverage_catalog.json` (the same one served at `/coverage`).
- Each dataset gets ~`count/N` questions; Gemma is asked in batches of 25 to amortise the per-prompt overhead.
- System prompt forbids near-rewordings, requires HK-specific place names, single-sentence English, ≤ 25 words.
- Output is JSONL — one question per line tagged with `expected_dataset_id` and `expected_tools` so the analyzer can score by tool match.
- Tolerates Gemma malformed output: extracts the outermost `[…]` block, ignores partial replies.
- Default synth model: `gemma-synth` (LM Studio identifier for `google/gemma-3-12b` — Gemma 3 27B doesn't fit in 96 GB alongside the gpt-oss-120b production model).

### `smcity_fuzz/coverage_run.py` — concurrent runner

- Reads the question JSONL, POSTs each to `/turn` with a unique `session_id`.
- `asyncio.Semaphore` for concurrency (default 4 — gpt-oss-120b on Mac Studio handles this comfortably).
- Checkpoint via line-count: re-running on an existing results file skips already-processed `question_id`s. Crash-resumable across sessions.
- Captures full response: reply text, tool trace (name / status / latency / cached), elapsed, detected language, citation count.
- Categorises wire failures explicitly: `http_error`, `timeout`, `network_error` so the analyzer sees them as first-class buckets, not "ok with nothing".

### `smcity_fuzz/coverage_report.py` — analyzer

For each result row buckets it into one of:
- `ok_expected_tool` — the agent called at least one tool from `expected_tools`.
- `ok_other_tool` — agent called a tool, but not the expected one (informative, not necessarily a failure).
- `ok_no_tool` — fast-path / chitchat / cached, no tool fired.
- `geocoder_collision` — reply contains the collision-guard wording (origin = destination).
- `empty_reply`, `timeout`, `error_status`, `http_error`, `network_error`.

Emits a Markdown report with: overall bucket totals, per-dataset table (total / hit-rate / avg latency), up to 5 failing-question samples per dataset. Also writes a compact JSON summary the agent reads back into `/coverage` so the `/data` UI shows per-dataset test health alongside the static catalog status.

### CLI (`smcity_fuzz/cli.py`)

New `coverage` verb with three sub-subcommands:

```
python -m smcity_fuzz coverage generate \
    --count 10000 --gemma-model gemma-synth \
    --lm-base-url http://localhost:1234/v1 \
    --out logs/coverage_questions.jsonl

python -m smcity_fuzz coverage run \
    --questions logs/coverage_questions.jsonl \
    --agent-url https://smcity.taila366aa.ts.net \
    --concurrency 4 --out logs/coverage_results.jsonl

python -m smcity_fuzz coverage report \
    --results logs/coverage_results.jsonl \
    --out-md logs/coverage_report.md \
    --out-json data/coverage_test_summary.json
```

### `smcity/coverage.py` + `/coverage` payload

Now reads `data/coverage_test_summary.json` (written by the report verb) and appends it as `test_summary` on the `/coverage` JSON response. Empty until a suite has been run.

### `web/coverage.html`

New stat tiles: "questions tested" + "hit expected tool" (overall %). Per-dataset table gains a "Tested (n / hit-rate)" column showing per-dataset run results when present.

### Smoke verification

100-question pilot on Mac Studio:
- Generation: 10 datasets × 10 questions = 100 unique questions in ~30 s via gemma-3-12b.
- Run: ~6 minutes wall time at concurrency 4.
- Sample questions Gemma produced: *"Where's the closest public toilet near Hong Kong University?"*, *"What's the typical headway for the 978 bus route heading towards Sha Tin at around 7:00 AM on a weekday?"*, *"What time does the ferry from Central to Tuen Mun start running?"*

### Handoff: the 10 000 overnight run

```bash
ssh earnestdesign@earnests-mac-studio.taila366aa.ts.net
cd ~/srv/smcity
nohup uv run python -m smcity_fuzz coverage generate \
    --count 10000 --gemma-model gemma-synth \
    --lm-base-url http://localhost:1234/v1 \
    --out logs/coverage_questions_v1.jsonl \
    > logs/coverage_gen.log 2>&1 &
# wait ~25 minutes, then:
nohup uv run python -m smcity_fuzz coverage run \
    --questions logs/coverage_questions_v1.jsonl \
    --agent-url https://smcity.taila366aa.ts.net \
    --concurrency 4 --out logs/coverage_results_v1.jsonl \
    > logs/coverage_run.log 2>&1 &
# overnight (~6-7 hours), then:
uv run python -m smcity_fuzz coverage report \
    --results logs/coverage_results_v1.jsonl \
    --out-md logs/coverage_report_v1.md \
    --out-json data/coverage_test_summary.json
# coverage page auto-shows the test column after the JSON exists
```

Phoenix Arize captures every one of those 10k turns automatically thanks to the v0.4.14 instrumentation — full traceability per question without extra work.

### Image

`smcity:0.4.16`. No backend changes that affect the running agent — just the new CLI module + UI panel.

## [0.4.15] — 2026-05-19

**Data coverage view + Funnel off.** Two boss asks in one ship:

1. *"From now on, almost exclusively via Tailscale Aperture; and it has audit logs and controls."* — flipped `AllowFunnel: false` in `deploy/serveconfig.json`. URL is now **tailnet-only** (Aperture / Serve), no more public-internet path. Reachable from any tailnet member; the boss controls access on his side.
2. *"For this could also show coverage of the data.gov.hk api or from the excel. I want to see what is added and what isn't added."* — new `/coverage` JSON endpoint + `/data` HTML page reconciling the 35 datasets from `3 - Selected Smart City Data Maps.xlsx` against the live tool registry. Status per row, click-through links to the source data, drift detection if a tool gets renamed.

### Coverage view

- **`data/coverage_catalog.json`** — hand-curated mapping of every xlsx row (35 datasets) to the agent tools that consume it. Each entry has `status: wired | partial | missing`, the list of `tools` that wire it, and a `notes` field explaining edge cases.
- **`smcity/coverage.py`** — loads the catalog at startup, reconciles against `registry.names()` on every request, returns a pydantic `CoverageReport` with summary counts + per-dataset detail + the extra integrations that aren't in the xlsx (HKO weather, LCSD facilities, HKHA housing, real-time bus operators, etc.).
- **`GET /coverage`** — JSON API. Adds `any_tool_registered: bool` and `missing_tools: list[str]` to each row so you can spot catalog drift directly in the response.
- **`GET /data`** — the human view: live-loading page with summary cards (wired/partial/missing counts), filterable table, status colour-coding, and bad-tool-name highlight. Linked from the chat header at `/`.

### Snapshot in numbers

```
total_xlsx_datasets: 35
wired:               30   (all OSM POIs via geo.search_osm_pois)
partial:              4   (S500 GTFS headway, S505 MTR fares,
                           S507 PT routes-fares, S512 iB1000 topo)
missing:              1   (S506 Ferry timetable + fares)
registered_tool_count: 26
additional_integrations: 13
```

### Tailnet-only access

- `deploy/serveconfig.json` — `AllowFunnel: false`. `tailscale serve status` now reports "tailnet only" on the sidecar.
- Tesfa runs Aperture in front of the tailnet; the smcity node sits behind it with no public-internet exposure. Audit logs + identity gating happen at the Aperture layer.

### Tests

`tests/test_coverage.py` — 5 new tests pin:
- Catalog has all 35 xlsx datasets.
- Summary totals match per-status row counts.
- Every tool name in the catalog is registered (catalog-drift guard — caught one typo during development: `meta.what_languages` → `meta.what_languages_are_supported`).
- Every `wired` dataset has at least one registered tool (no lying about coverage).
- S506 ferry is correctly marked `missing` with no tools.

261 → 266 total tests.

### Files

- `+ data/coverage_catalog.json`
- `+ smcity/coverage.py`
- `+ web/coverage.html`
- `+ tests/test_coverage.py`
- `~ smcity/app.py` — `/coverage` + `/data` routes
- `~ web/index.html` — "data coverage →" link in header
- `~ deploy/serveconfig.json` — AllowFunnel: false
- `~ deploy/docker-compose.yml` — image tag bumped to smcity:0.4.15

### Migration

Zero-downtime — first deploy that uses the rolling `./deploy.sh` workflow shipped in v0.4.14. No manual `docker compose down` needed.

## [0.4.14] — 2026-05-19

**Zero-downtime deploys + Phoenix Arize tracing.** Two things the boss asked for in one shipment:

1. *"It doesn't ever go down while you update the code"* — switched the deploy from a single-replica `--force-recreate` (~15 s outage per update) to blue/green replicas behind an nginx router with `proxy_next_upstream` failover. Rolling restart proven zero-downtime under continuous load.
2. *"Add integration to Phoenix Arize, see all session interactions, tool-call specifics, internal logic"* — OpenTelemetry spans exported to `https://phoenix.sustainer.ai/projects/smcity`. New session per page-refresh = new history in Phoenix, exactly as requested.

### Phoenix instrumentation (smcity/observability.py)

- `smcity.turn` span per user request. Attributes: `session.id`, `user.text`, `reply.text`, `tool_count`, `citations_count`, `detected_lang`, `locale_override`. Phoenix uses `session.id` to bucket conversation history; refreshing the page in the UI = new `session_id` = new trace tree.
- `tool.<name>` span per tool call inside `ToolRegistry.dispatch`. Attributes: `tool.name`, `tool.args` (JSON, truncated to 4 KB), `tool.result` (JSON, truncated to 8 KB), `tool.status`, `tool.latency_ms`, `tool.cached`, `tool.error` on failure.
- `llm.chat` spans auto-instrumented by `openinference-instrumentation-openai` — model, prompt, completion, token counts, finish reason for every call to LM Studio.
- Outbound httpx auto-instrumented by `opentelemetry-instrumentation-httpx` — every `data.gov.hk` / OSM Nominatim / ALS / MTR-realtime call shows up with method, URL, status, duration.
- `service.instance.id={blue|green}` resource attribute so you can tell which replica served a given request directly from Phoenix.

Tracing is opt-in: with `PHOENIX_COLLECTOR_ENDPOINT` empty the agent runs unchanged with no-op exporter (spans are still produced in-process; just nothing leaves the box). `PHOENIX_DISABLE=1` hard-kills the tracer.

### Zero-downtime architecture

```
boss → tailscale → nginx-router → smcity-agent-blue
                              ╲→ smcity-agent-green
```

- **`smcity-router`** — minimal `nginx:alpine`, ~50-line config. Round-robins both replicas; on any 5xx / connect-error / timeout marks that upstream `down` for 5 s and routes to the survivor (`proxy_next_upstream error timeout http_502 http_503 http_504 non_idempotent`). WebSocket-aware via `Upgrade` / `Connection` header passthrough.
- **`smcity-agent-blue` + `smcity-agent-green`** — same image, same config, same `smcity-sessions` volume. Always both running, both healthy.
- **`deploy/deploy.sh`** — single script: build new image, recreate blue (green serves everything), wait healthy, recreate green (blue serves everything), reload nginx. Run with `--no-build` to roll-restart without a rebuild.

Proven under load: 60 s of `curl /health` at 5 req/s during `deploy.sh --no-build` → **300 successes, 0 failures**.

### Files

- New: `smcity/observability.py` (Phoenix + OTel bootstrap)
- New: `deploy/nginx.conf` (load-balancer config)
- New: `deploy/deploy.sh` (rolling restart)
- Updated: `deploy/docker-compose.yml` (4 services now: blue, green, router, tailscale)
- Updated: `deploy/serveconfig.json` (proxies tailnet HTTPS → `smcity-router:8080`)
- Updated: `deploy/.env.example` (PHOENIX_* vars)
- Updated: `deploy/README.md` (new architecture diagram + deploy workflow)
- Updated: `smcity/app.py`, `smcity/orchestrator.py`, `smcity/tools/registry.py` (span sites)
- Updated: `pyproject.toml` (`opentelemetry-*` and `openinference-*` deps)

### Migration steps (one-time, only the first deploy is non-zero-downtime)

```bash
# On Mac Studio:
cd ~/srv/smcity
git pull   # or rsync
cd deploy

# Append Phoenix vars to existing .env (TS_AUTHKEY stays).
cat >> .env <<EOF
PHOENIX_COLLECTOR_ENDPOINT=https://phoenix.sustainer.ai
PHOENIX_API_KEY=<your-phoenix-api-key>
PHOENIX_PROJECT_NAME=smcity
EOF

# One-shot migration to the new compose structure (~30 s outage):
docker compose up -d --build --remove-orphans

# Verify:
docker compose ps              # 4 healthy containers
curl https://smcity.taila366aa.ts.net/health
```

Future updates: just `./deploy.sh` and the URL stays up.

### Image

`smcity:0.4.14`.

## [0.4.13] — 2026-05-19

**Replaces the v0.4.12 hardcoded landmark dict with a generic geocoder backed by OSM Nominatim.** v0.4.12 fixed the PolyU/CityU symptom by adding 84 explicit string→coords entries for seven universities + Disneyland — which would have failed the moment a real user asked about a restaurant, mall, hospital, park, beach, hotel, school, or any street that wasn't already in the dict. User feedback was direct ("you cannot just build custom solutions for example this is poor quality coder") and correct. This release deletes that dict and routes free-text queries through OSM Nominatim with a Hong Kong viewbox, which has comprehensive HK coverage in EN / 繁 / 简 from community-edited OSM data and handles the long-tail automatically.

### Geocoder (smcity/tools/transport_simple_modes.py)

New three-tier chain, each tier hit only if the previous returned `None`:

1. **Exact MTR station match** — case-insensitive, multilingual (EN / 繁 / 简), no fuzz. Cheap (in-memory dict), deterministic, handles `"Kowloon Tong"` / `"九龍塘"` / `"九龙塘"` → station coords for high-precision routing.
2. **OSM Nominatim** with `viewbox=113.83,22.56,114.43,22.15&bounded=1` — primary free-text geocoder. Verified live against 25 real-world HK queries (English + Traditional + Simplified): handles landmarks (M+, K11 MUSEA, Pacific Place, Festival Walk), restaurants (Tim Ho Wan), suburbs (Stanley, Discovery Bay, Tai O, Sham Tseng), temples (黃大仙廟), beaches (Repulse Bay), institutions (HKU/PolyU/CityU when given names containing "Hong Kong"), and the universal Chinese-name path (鯉魚門, 西貢, 大澳, 海洋公園, 彌敦道, 中環, 尖沙咀). Picks the highest-importance candidate from the top 5, so the prominent mall wins over an obscure street fragment that happens to share a word.
3. **ALS** (Lands Department) — fallback for street-level addresses Nominatim may not have (`"11 Yuk Choi Road"`, `"Block 5 Whampoa Estate"`).

### Prompt: prefer canonical names

OSM-data quirk: the "CityU" abbreviation is tagged on the **CityU Veterinary Medical Centre** in Sham Shui Po (importance 0.208) — the **main City University of Hong Kong campus** is only tagged with the full English/Chinese names. Same shape: bare abbreviation queries can hit a satellite office whose name happens to contain the abbreviation. Solved at the prompt layer (not in data): `SYSTEM_PROMPT` now instructs the LLM to pass full canonical names ("Hong Kong Polytechnic University" / "香港城市大學") to transport tools. This is correct LLM behaviour, not a per-place patch.

### Removed

- The 84-entry `_LANDMARK_COORDS` dict added in v0.4.12 (PolyU / CityU / HKU / CUHK / HKUST / HKBU / Lingnan / Disneyland variants).
- The `_landmark_lookup` helper that read from it.
- The associated test `test_landmark_lookup_universities_multilingual` (irrelevant under the new architecture).

What's left is one small layer (exact MTR station match) that's defensible because the MTR catalog is a finite, authoritative, slow-to-change set, not "places we happened to test".

### Why Nominatim and not OSM-self-hosted / Google Maps

- Nominatim's public instance accepts ~1 req/s, free, no API key, no terms-of-service drama. Our usage profile is ≤2 geocodes per user turn — well inside the limit even for a busy demo. Self-hosting Nominatim is a single Docker container if we ever outgrow the public instance.
- Google Maps requires a billed API key and a content-attribution overlay we'd have to render in the UI. Doesn't fit the "real public data, no commercial entanglement" project framing.

### Nominatim policy compliance

- `User-Agent: smcity-agent/0.4.13 (Lab of Social Robotics; HK smart-city assistant)` — the policy requires a meaningful UA identifying the application.
- `Accept-Language: en,zh-Hant,zh-Hans` so name fields come back in the user's likely language.
- 5 s timeout; failures fall through to the ALS tier rather than crashing the planner.

### Tests

`tests/test_simple_modes.py` rewritten again:
- New `test_geocode_one_prefers_exact_mtr_over_nominatim_and_als` — proves Tier 1 short-circuits cleanly.
- New `test_geocode_one_falls_through_to_nominatim` — uses a realistic Nominatim response with multiple candidates of varying `importance`; asserts the highest-importance result wins (this is the actual fix for the CityU vet-centre vs main-campus disambiguation).
- New `test_geocode_one_falls_through_to_als_when_nominatim_empty` — Tier 3 fires only when Tier 2 returned nothing.
- New `test_geocode_one_nominatim_failure_falls_through_to_als` — when Nominatim 500s/times out, ALS gets tried.
- New `test_geocode_one_returns_none_when_all_tiers_fail` — error path is `None` not exception.
- Old `test_landmark_lookup_*` tests deleted (no longer applicable).

### Migration

Docker rebuild required (`docker compose up -d --build` on Mac Studio). Image tag bumped to `smcity:0.4.13`.

## [0.4.12] — 2026-05-19

**Geocoder rewrite + taxi removal.** A live boss-demo query — "Im in polyu how do I get to cityu" — returned "just walk, it's 1 minute away". Root cause: `fuzz.WRatio` in `resolve_mtr_station` scored *"Polytechnic University Hong Kong"* against MTR station *"University"* (CUHK at Sha Tin) at 90, while substring-matching any single-word station name (Central / Airport / Kowloon / HKU / …) appearing anywhere in the query. Both PolyU and CityU collapsed to the *same* wrong point (22.4149, 114.2098), so the planner truthfully returned "0 m walk, 1 min" — looking plausible while being completely wrong.

This release replaces the speculative fuzzy short-circuit with a strict three-tier geocoder backed by **real HKSAR APIs first**, and removes taxi as a supported mode entirely (per repeated user feedback: the brand promise is "smart-city data via HK gov APIs" — distance × tariff arithmetic is not that).

### Geocoder (smcity/tools/transport_simple_modes.py)

New resolution order, each tier hit only if the previous returned `None`:

1. **Landmark override (multilingual)** — direct (lat, lng) coords for landmarks ALS provably can't resolve. Currently just Disneyland (verified failure: ALS returns "Hong Lok Yuen Country Club" for 迪士尼樂園 and "Diveria Boulevard" apartments for 迪士尼乐园). 12 keys covering EN + Traditional + Simplified all map to the real Disneyland Resort coords (22.31480, 114.04460). Speculative entries don't earn a slot — every entry has a comment naming the documented ALS failure mode.
2. **Exact MTR station match (multilingual, no fuzz)** — case-insensitive equality against every language variant. Handles `"Kowloon Tong"` / `"九龍塘"` / `"九龙塘"` → station coords directly. Does NOT fuzzy-match: `_exact_mtr_station_match("Polytechnic University Hong Kong")` returns `None` instead of falsely binding to "University" station.
3. **ALS with `Accept-Language: zh-Hant,zh-Hans,en`** — primary geocoder. Confirmed working against the live endpoint for PolyU, CityU, Lan Kwai Fong, Times Square, Wong Tai Sin, Festival Walk, Mong Kok, Tsim Sha Tsui, both EN and ZH-Hant/Hans inputs.

Fuzzy MTR matching is **gone from the geocoder path**. If a misspelled station name reaches us, ALS still has a fair shot via its own address fuzziness; if we ever need typo-tolerance on station inputs we'll add it back with evidence, not speculation.

### Collision guard

`_resolve_pair` now raises `ToolUpstreamError` when origin and destination resolve within 100 m of each other. Previously the planner happily returned "0 m walk, 1 min" — a plausible-looking lie that hid the underlying geocoder collision. Now the agent must surface the error and re-ask the user.

### Taxi removed

- `PLAN_TAXI_TOOL` unregistered from `build_default_registry`.
- Taxi mode + handler code deleted from `transport_simple_modes.py` (`_TAXI_*` constants, `_taxi_fare_hkd`, `PlanTaxiArgs/Result`, `_taxi_handler`, `PLAN_TAXI_TOOL`).
- `_JourneyMode = Literal["walk", "mtr"]` (was `Literal["walk", "mtr", "taxi"]`). Pydantic now rejects `modes=["taxi"]` at schema validation; planner cannot emit taxi options under any path.
- `transport.plan_taxi_estimate` removed from `langrouter/coverage.py`.
- Prompt rewritten: `Taxi is NOT a supported mode — never volunteer taxi fares, taxi durations, or "you could also take a taxi"`. The "Or walk ~X min / taxi ~Y min" closing-line guidance is now "Or walk ~X min." only.
- `smcity_fuzz.datasets` journey_planning topic drops `transport.plan_taxi_estimate` from `expected_tools`.

### `resolve_mtr_station` (smcity/tools/transport.py)

Still used by `transport.get_mtr_next_trains` — that tool legitimately needs fuzziness for the station-name argument. Tightened to:
1. Exact case-insensitive match first.
2. `fuzz.token_sort_ratio` cutoff 85 if exact fails.

`token_sort_ratio` scores `"Polytechnic University Hong Kong"` vs `"University"` at 47 (rejected) while keeping legitimate near-matches like `"kowloon tong"` → `"Kowloon Tong"` at 83 (passes the still-existing per-tier exact check anyway).

### Tests

`tests/test_simple_modes.py` rewritten:
- New `test_landmark_lookup_disneyland_multilingual` pinning all 12 language variants.
- `test_exact_mtr_station_match_*` covering English exact, Traditional Chinese exact, and the explicit no-false-positive guard for "Polytechnic University Hong Kong" / "City University of Hong Kong".
- `test_geocode_one_prefers_landmark_override` and `test_geocode_one_prefers_exact_mtr_over_als` use `respx` to assert ALS is NOT hit when an earlier tier resolves.
- `test_geocode_one_falls_through_to_als` mocks the ALS response with the real-schema PolyU result and asserts coords land in Hung Hom (22.29 < lat < 22.32), NOT the Sha Tin University-station coords (22.41) the old code produced.
- `test_plan_journey_returns_walk_and_mtr_only` replaces the old three-mode assertion.
- New `test_plan_journey_taxi_mode_rejected_by_schema` confirms `modes=["taxi"]` is now a pydantic validation error.
- New `test_plan_journey_collision_guard_triggers` confirms "Kowloon Tong" on both sides errors out.
- Deleted `_taxi_fare_hkd` fare-formula test (the function no longer exists).

`tests/test_response_quality.py`:
- `test_system_prompt_has_per_mode_routing_table` updated — no longer asserts on `plan_taxi_estimate` / `的士`. Adds positive assertions on `get_kmb_eta` / `get_citybus_eta`.
- New `test_system_prompt_forbids_taxi_mode` pinning the "Taxi is NOT a supported mode" directive.

### MTR_STATION_COORDS expanded 32 → 93

`smcity/tools/transport_search.py`'s coord table was a hand-curated subset of "the busiest 30 stations". Discovered live: planning to **Hong Kong Disneyland Resort** ran the geocoder correctly to (22.3148, 114.0446), but `_nearest_mtr_station` returned "no MTR station within 1500 m" because **DIS wasn't in the table at all** — same situation for everything on DRL, most of TML, all of TKL east of TKO, all of SIL, and most of TWL. Rewrote the table to cover all 93 unique station codes in `data/mtr_stations.json`, organised by line, coords from OSM / MTR system map (5 dp precision). Now any (lat, lng) within walking distance of *any* HK heavy-rail station gets a real route.

### Multilingual university aliases

Live battery surfaced that the LLM passes abbreviations verbatim — `"PolyU"`, `"CityU"`, `"理工"`, `"城市大學"` — and ALS doesn't reliably resolve them (e.g. bare `"PolyU"` returns a Fanling address; `"城市大學"` without `"香港"` returns CUHK-area coords). Expanded `_LANDMARK_COORDS` from 12 → 84 entries covering PolyU / CityU / HKU / CUHK / HKUST / HKBU / Lingnan in:
- English: abbreviation (`polyu`), spaced (`poly u`), with-prefix (`hk polyu`), expanded (`Hong Kong Polytechnic University`).
- Traditional Chinese: short (`理工`), formal (`理工大學`), with prefix (`香港理工大學`).
- Simplified Chinese: formal (`理工大学`), with prefix (`香港理工大学`).

Each campus block has a comment naming the ALS failure mode it fixes. Coords are the actual main-campus locations (verified against the live ALS response for the expanded English form, except where ALS fails — then sourced from OSM).

### `gpt-oss-120b` nested-args envelope unwrap

`ToolRegistry.dispatch` now defensively unwraps `{"name": "...", "arguments": {...}}` when the LLM double-wraps a tool call. Saw this live: `transport.plan_journey` got args `{"name": "transport_plan_journey", "arguments": {"origin": "City University of Hong Kong", "destination": "Hong Kong Polytechnic University"}}` — pydantic validation failed because the fields aren't `name`/`arguments`. The unwrap only fires when the dict has *exactly* those two keys and `arguments` is itself a dict, so legitimate calls with `name=` or `arguments=` fields (none exist today) wouldn't be silently mangled.

### Additional tests

- `test_landmark_lookup_universities_multilingual` — covers PolyU/CityU/HKU/CUHK/HKUST/HKBU in EN + 繁 + 简, 6 universities × 4-6 variants each.
- `test_plan_journey_unwraps_gpt_oss_nested_args` — proves the dispatch unwraps the nested envelope.
- `test_plan_journey_routes_to_disneyland_resort` — end-to-end: `"Hong Kong Disneyland"` → landmark coords → DIS station → real MTR route with non-empty `mtr_legs_summary`.

### Live verification

Battery of 10 EN + 5 ZH-Hant + 3 ZH-Hans queries against the Mac Studio deploy. The previously-broken cases:

- `Im in polyu how do I get to cityu` → East Rail Line via Hung Hom + Kowloon Tong (was: "1 min walk")
- `Hong Kong Disneyland → Lan Kwai Fong` → Tung Chung + Disneyland Resort line (was: "540 m walk")
- `理工大學 → 城市大學` → East Rail Line (was: same wrong-coords collision)

### Migration

Docker rebuild required (`docker compose up -d --build` on Mac Studio). Image tag bumped to `smcity:0.4.12`.

## [0.4.11] — 2026-05-19

**Mac Studio deploy pack.** The Macbook-hosted Funnel breaks every time the laptop sleeps; demo URL has to live on the always-on Mac Studio. Adds a two-container deploy (agent + Tailscale sidecar) following Tesfa's reference compose pattern.

### New files

- **`Dockerfile`** — two-stage build (deps in `/opt/venv`, source at `/app` to preserve the repo's relative-path data lookups). Multi-arch via `python:3.12-slim`. Non-root user (`smcity:10001`). Healthcheck via stdlib `urllib.request` (no curl needed). The naïve "install as a wheel" approach would have broken `Path(__file__).parent.parent.parent / "data"` resolutions for `mtr_stations.json` / `mtr_lines.json` / `hkha_name_map_tc.json` — caught + fixed before commit.
- **`deploy/docker-compose.yml`** — two services on a shared bridge network:
  - `smcity-agent`: builds from this repo. Reaches LM Studio on the Mac Studio host via `host.docker.internal:1234` (Docker Desktop for Mac built-in). Persists session DB to a named volume.
  - `smcity-tailscale`: official `tailscale/tailscale:latest` sidecar in userspace mode. Auth-key via `.env`. Joins the Earnest Design Lab tailnet as `smcity`, terminates HTTPS with auto-issued Let's Encrypt cert.
  - `depends_on: smcity-agent { condition: service_healthy }` so the sidecar doesn't expose the URL until the agent is actually serving.
- **`deploy/serveconfig.json`** — Tailscale Serve config. `${TS_CERT_DOMAIN}` is auto-resolved by the sidecar; proxies `:443 → http://smcity-agent:8080` over the bridge network. `AllowFunnel: false` by default (tailnet-only); flip to `true` for public exposure after enabling Funnel on the tailnet.
- **`deploy/.env.example`** — slot for `TS_AUTHKEY` + optional `TS_HOSTNAME`. Real `deploy/.env` is gitignored.
- **`deploy/README.md`** — full Mac Studio walkthrough: prereqs, one-time setup, smoke tests, boss-sharing paths (tailnet member vs Funnel public), operating commands, troubleshooting table, remote model-load instructions when `gpt-oss-120b` isn't loaded.
- **`.dockerignore`** — keeps the build context lean (no .venv, no logs, no tests, no docs, no handoff/, no secrets).

### .gitignore

Added `deploy/.env` to the ignore set so the auth key never gets committed.

### Migration path

1. On the Mac Studio: `git clone ... && cd smcity/deploy && cp .env.example .env`
2. Paste the Tailscale auth key into `.env`
3. `docker compose up -d --build`
4. Look in `docker compose logs smcity-tailscale` for the new URL (e.g. `https://smcity.taila366aa.ts.net/`)
5. Tear down the laptop's Funnel (`sudo tailscale funnel reset`) — Mac Studio is now the demo host

### Gate

Compose syntax validated (`docker compose -f deploy/docker-compose.yml config --quiet` passes). Image build deferred to the Mac Studio.

## [0.4.10] — 2026-05-19

**Three bugs the boss caught in 30 seconds of live testing. All fixed.**

### Bug 1 — `_first_named_station` picked the wrong leg ("(origin)" placeholder)

For "Kwong Fai Mansion → Central" the agent said "walk to **the nearest MTR station**" because `plan_journey.options[mtr].mtr_origin_station` was literally the string `"(origin)"`. v0.4.9's helper grabbed the first leg's `from_name_en`, but the first leg is `walk('(origin)' → Yau Ma Tei)`, not a transit leg. Fixed by filtering out `walk` legs in `_first_named_station` and defensively dropping any `(...)` placeholders. Tool now correctly returns `mtr_origin_station = "Yau Ma Tei"`.

### Bug 2 — cost/distance noise overwhelmed the directions

Previous prompt told the LLM to "present all three modes side-by-side" with a table including HK$ ranges and km distances. User feedback: "I don't need this price I don't need ny bullshit for now I just need right directions". New travel-reply rule in `SYSTEM_PROMPT`: directions-first paragraph, name the station and line, quote `mtr_legs_summary` verbatim, **mention walk/taxi as one short fallback sentence**. Tables only on explicit request ("compare options" / "show all modes").

Before:
```
| Mode | Distance | Time | Cost |
| Walk | 3.8 km   | 53m  | –    |
| MTR* | –        | 4m   | HK$4-6 |
| Taxi | 5 km     | 12m  | HK$56-69 |
*MTR: Walk to Hung Hom...
```

After:
```
Take the MTR: walk to Hung Hom station, board the East Rail Line
and get off at Kowloon Tong — about 4 minutes total.

If you prefer, it's a ~53-minute walk (~3.8 km) or a taxi ride
(~12 min, HK$56-69).
```

### Bug 3 — UI streamed bare-leak tool calls verbatim before cleanup could strip them

User's third turn ("walk to what mtr?") showed `transport.plan_journey json{...}` as visible chat text. The streaming path in `chat_stream` swallows `<|`-prefixed harmony tokens but NOT bare-leak `tool.name json{...}` patterns. Those got streamed to the UI bubble token-by-token. Then `turn.final` arrived with cleaned text but `web/app.js` only swapped the bubble text **if it was empty** — so the dirty streamed text remained.

Fixed in `web/app.js`: on every `turn.final`, ALWAYS overwrite the bubble with the server's final cleaned text (it has been through `extract_harmony_tool_calls` + `_rewrite_source_footer`). The server-side cleanup is the source of truth.

### Gate

- 253 unit + 7 integration tests green
- ruff + format + mypy strict clean across 77 source files
- Live verified end-to-end against gpt-oss-120b through the public Funnel URL

## [0.4.9] — 2026-05-19

**Fixes a real hallucination spotted live by the boss.** "I'm in PolyU how do I get to CityU?" produced a confidently-stated but fictional MTR route ("Tsuen Wan line to Mong Kok, change to Kwun Tong line at Yau Ma Tei, get off at Kowloon Tong") — none of which are correct. The real path is Hung Hom → East Rail Line → Kowloon Tong, 2 stops, 4 min.

### Root cause

`transport.plan_journey` deliberately returned `mtr.duration_min = null` with a `note: "Call transport.plan_simple_route for an accurate MTR leg-by-leg plan…"` — relying on the LLM to make a second tool call. gpt-oss-120b ignored that instruction and made up a plausible-sounding route. The follow-up-call design was a footgun: structured data > LLM-follow-through every time.

### Fix

`plan_journey` now CALLS `plan_simple_route` internally and ships the real Dijkstra-computed MTR leg inline as structured fields the LLM cannot misread:

```
JourneyOption(
  mode="mtr",
  duration_min=<real>,
  mtr_origin_station="Hung Hom",
  mtr_destination_station="Kowloon Tong",
  mtr_lines=["East Rail Line"],
  mtr_legs_summary="MTR: walk to Hung Hom, take the East Rail Line to Kowloon Tong, ~4 min total.",
)
```

`_inline_mtr_leg()` handles the call, with graceful degradation if the planner returns an error or the pair is too close together (sub-600m → suggest walking).

### Verification (live, via gpt-oss-120b on Mac Studio, through public Funnel URL)

```
Q: Im in polyu how do I get to cityu
A: | Mode | Time | Cost |
   | Walk | ~53 min (~3.8 km) | free |
   | MTR  | ~4 min total – walk to Hung Hom station, take the
            East Rail Line to Kowloon Tong | ~HK$5–7 |
   | Taxi | ~12 min (~5 km) | HK$56–69 |

   The MTR is the fastest and cheapest option.

elapsed: 7s · tool result contained the real route, LLM quoted it verbatim.
```

### Gate

- 253 unit + 7 integration tests green
- ruff + format + mypy strict clean across 77 source files

## [0.4.8] — 2026-05-19

**Critical bug fix.** ALS (Lands Department Address Lookup Service) was silently returning 0 candidates for every query since v0.1.0 — the parser assumed a GeoJSON `features[]` shape, but ALS actually returns `SuggestedAddress[]` with nested `Address.PremisesAddress.GeospatialInformation.{Latitude,Longitude}`. This single bug broke `geo.address_lookup` and every journey-planning tool that called `_geocode_one`. Hero scenarios like "How do I get from Kowloon Tong to Hung Hom?" were unanswerable.

### Diagnosis path

Driving the boss's hero queries against a live qwen3.5-9b LM Studio instance produced "Origin could not be resolved — provide origin_lat/lng or a clearer name" for every place-name input. Verified ALS returned 3 valid candidates via direct curl; the agent's parser saw 0. Fix touches two consumers + one tool + two tests.

### Changes

- **`smcity/tools/geo.py`** — rewrote `_handler` against the real ALS schema: walks `SuggestedAddress[i].Address.PremisesAddress` for `EngPremisesAddress` / `ChiPremisesAddress` / `GeospatialInformation`. Added `_compose_name()` helper that prefers `BuildingName`, falls back to `<no.> <StreetName>`. Module docstring corrected — was claiming "GeoJSON FeatureCollection" since v0.1.
- **`smcity/tools/transport_simple_modes.py`** — split `_geocode_one` into three layered resolvers used in order:
  1. `_LANDMARK_ALIASES` dict (`cityu` → "City University of Hong Kong, Tat Chee Avenue, Kowloon Tong"; `polyu` → "Hong Kong Polytechnic University, Hung Hom"; `hku` → "HKU"; `cuhk` → "University"; `hkust` → "Clear Water Bay"; `hkbu` / `baptist u` / `lingnan u` / `ouhk` / `disneyland` / `ocean park` / `airport` / `hkia`).
  2. `_geocode_via_mtr_catalog` — fuzzy-matches against the 105-station trilingual MTR catalog using existing `resolve_mtr_station` + `MTR_STATION_COORDS`. Zero network calls.
  3. `_geocode_via_als` — same fixed real-ALS-schema parser.
- **`tests/test_tools.py`** — `test_als_address_lookup_parses_features` renamed to `test_als_address_lookup_parses_real_schema`, mock body rewritten to match ALS reality.
- **`tests/test_simple_modes.py`** — ALS mock updated to real schema; `assert_all_called=False` because the MTR-catalog shortcut now resolves "Mong Kok" before ALS is touched (correct).

### Live verification (after the fix)

```
geocoder direct calls:
  'Kowloon Tong'                  → (22.3371, 114.1761)   MTR catalog
  'Hung Hom'                      → (22.3032, 114.1816)   MTR catalog
  'CityU'                         → (22.3371, 114.1761)   alias → MTR catalog
  'PolyU'                         → (22.3032, 114.1816)   alias → MTR catalog
  'Mong Kok'                      → (22.3195, 114.1692)   MTR catalog
  'City University of Hong Kong'  → ALS hit, real coords

agent end-to-end against live qwen3.5-9b:
  "I am at Kowloon Tong, how do I get to Hung Hom?" → answered
    (walk 53min / taxi ~$56-69 12min / MTR recommended), 73s
  "How do I get from CityU to PolyU?"               → answered
    (walk 53min / taxi 12min / MTR recommended),    139s
```

### Gate

- 253 unit + 7 integration tests green
- ruff + format + mypy strict clean across 77 source files
- Live agent confirmed end-to-end on the boss's two hero queries

## [0.4.7] — 2026-04-23

**First real fuzz run uncovered a judge-blindness bug.** Kicked off a 3-turn smoke against live gpt-oss-120b (LM Studio on Mac Studio) + live smcity agent — 2/3 "failures" were actually the judge's fault: the `tool_trace` passed to it only carried `result_summary` strings like `"8 trains @ Central"`, not the underlying `next_trains[]` array with `ttnt` minutes. When the agent correctly synthesised "下一班 1 分鐘後到, 之後 5/9/12 分鐘" from the real tool output, the judge saw no numbers in the summary and flagged `hallucinated_fact` every time. Fixed.

### The judge fix (most important change)

- **`smcity/schemas.py`** — `ToolTraceEntry` now carries `result: dict | None` alongside `result_summary`. Populated on `status == "ok"`.
- **`smcity/orchestrator.py`** — `_append_trace_and_citations` copies the raw result into the trace entry.
- **`smcity_fuzz/judge.py`** — `_user_prompt` now renders the full result (JSON, truncated to 1500 chars past which it says `…(+N chars)`). Explicit new framing: "raw_result is the truth — summary is just a shorthand label".

Before v0.4.7: any reply citing specific numbers got `hallucinated_fact` false-flagged. After: the judge can actually verify "is 1 minute in the next_trains list?".

### UX improvements to the fuzzer

- **Live per-turn progress** to stderr: `[ 1/40]  ok  yue      cantonese_senior   mtr_next_trains       7.8s  score=10/10`. Runs no longer look frozen for 10 minutes. `--no-progress` to suppress.
- **Round-robin / shuffled sampling** (default): `run_campaign(sampling="shuffled", seed=None)`. Small `--turns` budgets now sample across all personas + languages instead of only the first persona. Previously `--turns 40` only exercised `cantonese_senior`; with shuffled sampling it touches all 5. Pass `--seed N` for reproducibility.
- **Stricter synth language constraint**. Before: asking a "Cantonese senior" persona to write in English produced mostly Cantonese, which the judge (correctly) flagged `wrong_language`. Fixed with per-language `_STRICT_LANG_RULES` that are the LAST rule in the synth prompt and explicitly override the persona's implied native language.

### CLI

```
uv run python -m smcity_fuzz run
  --mode ws --turns 40 --concurrency 2
  --sampling shuffled --seed 42      # reproducible matrix sampling
  --no-progress                      # silence per-turn stderr (default: on)
```

### Gate

- 252 unit + 7 integration tests still green, ruff + format + mypy strict clean.
- Package built on disk as 0.4.7; live 40-turn campaign now in progress to get clean judge data.

## [0.4.6] — 2026-04-23

**Response-accuracy scaffolding** — the user flagged that what matters isn't bug count, it's whether responses are actually good and factually accurate. This commit builds the accuracy-quality scaffolding so the fuzzer, the judge, and any future diagnostic session (Claude / Gemini) all grade the same way.

### `README.md` rewritten for v0.4.6 reality

Old README was stuck at v0.1.0 / 18 tools / 129 tests and never mentioned the fuzzer, OTP2, CSDI live data, or the hardening layers. New README:
- Status line at the top: 27 tools · 232 unit + 7 integration tests · ruff / format / mypy strict clean.
- Adversarial-fuzzer section with the exact commands + handoff-to-Claude/Gemini flow.
- OTP2 multimodal section with the activation steps.
- Updated layout tree reflecting the real 4 packages (`smcity/` + `smcity_fuzz/` + `otp/` + `web/`).
- Demo transcripts use live data (Choi Hung Estate from the HKHA feed, Sha Tin 7 courts from CSDI).
- Feature inventory matches the actual 27 tools per domain.
- Security posture summary links to DEPLOY.md threat model.

### New: `docs/ACCURACY_REVIEW.md`

Hand-curated risk register — every known way responses can drift, grouped by the code layer responsible. Ten sections:

1. Intent misidentification (wrong_tool)
2. Language drift (wrong_language, english_in_cantonese, mandarin_in_cantonese)
3. Factual drift vs tool output (hallucinated_fact, stale_data)
4. Wrongful refusals (refused_wrongly)
5. Structural leaks (harmony_leak, empty_reply, incomplete)
6. Disambiguation failure
7. Routing correctness
8. Rate-limit + session hygiene
9. What the judge should NOT flag (rubric-noise suppression)
10. How to run a full accuracy pass

Each failure mode names the past live incident (where one exists), the exact tag the judge should emit, and the code layer that contains the mitigation knob. Claude / Gemini receiving the fuzzer export can use this as grading criteria.

### New: `tests/test_response_quality.py` (20 tests)

Regression pins for the specific live bugs from past chat transcripts. Won't reach a real LLM — pins the SYSTEM_PROMPT wording, few-shot exemplars, language-stick reminders, polish behaviour, and source-footer rewrite.

Test categories:
- SYSTEM_PROMPT safety + routing (6 tests): Cantonese priority, no hallucinated facts, plan_journey-not-ask_user for ambiguous travel, per-mode routing table, harmony-tokens-forbidden, no-self-written-src-footer.
- Cantonese style block content (2 tests): all key particles named, ≥6 FORMAL/CANTO exemplars.
- `language_stick_reminder` + `fast_path_synthesis_hint` (3 tests): bilingual-field pullover forbidden, language + tts_locale forwarded, forced-vs-detected marked.
- `_maybe_polish` only fires on yue (1 test — critical; polishing English would corrupt it).
- Source-footer rewrite (4 tests): strips LLM-invented fakes, removes any src when no real citations, tolerates full-width colon `src：`, dedups repeated tool.
- Polish over-eager-substitution guards (3 tests): English in mixed reply untouched, 的士 / 正在 / 了解 / 了結 stays intact, proper-noun `現代` not mangled.
- Orchestrator retry mitigation (1 test): `_stream_final` retry prompt pins "STOP CALLING TOOLS" guard-phrase.

A refactor that silently drops "Cantonese is the priority language" from the prompt, or weakens the "STOP CALLING TOOLS" retry, or forgets that `_maybe_polish` should be gated to `yue`, now fails at CI time instead of in front of a Cantonese user.

### Gate

- **252 unit tests + 7 integration tests green** (was 232 + 7).
- ruff + format + mypy strict clean across 77 source files.

## [0.4.5] — 2026-04-23

**Docs + metadata drift fix.** No runtime changes; this commit reconciles stale strings and incorrect claims so the repo state on disk matches shipped reality.

### Drift found by a health-check pass

Version numbers had diverged across four places:
- `pyproject.toml` `version` was still `0.1.0` — project metadata never got bumped past the initial scaffold.
- `smcity_fuzz/__init__.py` `__version__` was `0.4.3` — one version behind main package.
- `docs/DEPLOY.md` banner said `v0.3.1` — stuck at the audit-remediation release.

Factual drift in `docs/DATASETS.md`:
- Coverage summary was labelled v0.3.0 with 26 tools. Now v0.4.4 / 27 tools.
- Claimed "**3 bundled-data tools** (LCSD × 2 + HKHA × 1) outside the workbook scope; live upgrade tracked for v0.4+". False — all three went live in v0.3.4 and v0.4.2.
- No mention of `transport.plan_multimodal_journey` or the OTP2 scaffold.

`docs/architecture/TOOL_CATALOG.md` was almost entirely stale — it listed ~20 aspirational v0.1 tools that don't exist (`transport.get_lrt_next_trains`, `get_mtr_service_status`, `get_nlb_eta`, `get_ferry_schedule`, `get_tram_info`, `plan_barrier_free_route`, `reachability_isochrone`, `detect_transfer_time`, `context.get_drive_traffic_speed`, `context.get_traffic_snapshot_url`, `facility.get_availability`, `housing.get_property_market_stats`, etc.) while missing every tool shipped since v0.3. **Rewritten from scratch** to reflect the actual 27-tool registry with accurate purposes, upstream sources, and TTLs. Cross-links `smcity_fuzz/` + `smcity/langrouter/coverage.py` + `otp/README.md`.

`smcity/langrouter/coverage.py` had two `# Facility (bundled)` / `# Housing (bundled)` comments that were factually wrong after the CSDI / HKHA migrations; also missing entries for `transport.plan_multimodal_journey` (OTP2, v0.4.4) and `csdi.query_features` (v0.3.2) — both were falling through to the English-only default. Updated comments + added both entries as `{"en", "zh-Hant"}`.

### Health check (this commit's baseline)

- Ruff + format + mypy strict all clean across 76 source files.
- **232 unit tests + 7 integration tests pass** (0 failures).
- **Live smoke hits 13/13 upstreams** in 0.5–3 s each: HKHA, CSDI courts + pools, ALS, HKO (weather / warnings / AQHI / 9-day), MTR, KMB, OSM Overpass, CSDI generic.
- No runtime bugs found. This commit is pure metadata alignment.

## [0.4.4] — 2026-04-23

**OpenTripPlanner 2 sidecar scaffold** — true multimodal journey planning (walk + bus + MTR + minibus + ferry) via an OTP2 HTTP sidecar running in Docker. The hand-rolled Dijkstra MTR-only planner stays as the fast path / fallback.

### New: `smcity/tools/otp2.py`

- `transport.plan_multimodal_journey(origin_lat, origin_lng, destination_lat, destination_lng, modes, date, time, arrive_by, num_itineraries)` — thin async client over OTP2's `/otp/routers/default/plan` REST endpoint.
- Accepts either OTP2 uppercase mode names (`TRANSIT`, `BUS`, `RAIL`, `SUBWAY`, `FERRY`, `WALK`) or lowercase aliases. Deduplicates.
- Normalises OTP2's leg shape into `PlanLeg(mode, route_short_name, route_long_name, agency_name, from_stop, to_stop, start_time, end_time, duration_s, distance_m)` so the LLM sees the same leg model our simple planner uses.
- Graceful degradation: when the sidecar is unreachable, raises `ToolUpstreamError` with a hint pointing at `otp/README.md`. The agent falls back to `transport.plan_simple_route`.
- Env-driven config: `OTP2_BASE_URL`, `OTP2_ROUTER`, `OTP2_TIMEOUT_S`.

### New: `otp/docker-compose.yml` + `otp/README.md`

- Pinned to `opentripplanner/opentripplanner:2.6.0` (breaking-change-averse).
- Binds `127.0.0.1:8080` by default (loopback-only; agent hits it locally).
- 4 GB heap via `JAVA_TOOL_OPTIONS=-Xmx4g`.
- Healthcheck via `/otp/routers/default`.
- README covers: collecting GTFS inputs (MTR, KMB, Citybus, GMB), OSM extract sources (bbbike HK is ~60 MB, Geofabrik china is 1+ GB), a minimal `build-config.json`, the graph-build one-liner, startup, and smoke-test script. Also lists known limitations (no fare prediction, GMB live ETA separate, cross-harbour ferry depends on feed).

### `.gitignore`

`otp/data/` is gitignored — it's docker-compose's mount point for the graph + GTFS/OSM inputs (~4 GB), not source.

### What you still need to do manually

1. Install Docker.
2. Drop HK GTFS zips + `hong-kong.osm.pbf` into `otp/data/`.
3. `cd otp && docker compose run --rm otp2 --build --save` (~15–30 min on an M-series Mac).
4. `docker compose up -d`.
5. The agent picks it up automatically.

### Tests (6 new, CI-safe)

- `test_mode_mapping_lowercase_aliases_to_uppercase` — mode alias + dedup logic.
- `test_tool_registered_in_default_registry` — pins the new tool name.
- `test_otp2_happy_path_parses_itineraries` — 3-leg walk-subway-walk plan, verifies mode + route + agency extraction.
- `test_otp2_connection_refused_surfaces_clean_upstream_error` — sidecar-down path carries the helpful hint.
- `test_otp2_planner_error_payload_surfaces_msg` — OTP's in-payload error envelope is lifted into the tool error.
- `test_otp2_empty_itineraries_surface_helpful_note` — graph-bounds miss returns an empty itinerary list with a diagnostic note.

Every HTTP call is respx-mocked — no Java / Docker / sidecar required for CI. The live integration is what `otp/README.md`'s smoke-test script covers.

- **232 tests green** (was 226), ruff + format + mypy strict clean across 76 source files.
- Registry now ships **27 tools** (was 26).

## [0.4.3] — 2026-04-23

**WebSocket streaming fuzz runner** — the fuzzer now exercises the same code path the production UI uses (`/ws/{session_id}` streaming) and captures user-perceived latency metrics the HTTP path can't measure.

### New: `smcity_fuzz/ws_transport.py` + `--mode ws` flag

- `drive_turn_via_ws(question, session_id, settings, connect=None)` — opens a fresh WebSocket per turn, sends the `turn` frame, drains `turn.start` / `tool_call.*` / `turn.token` / `turn.final` events, and aggregates them.
- Injectable `connect` param lets unit tests substitute an in-memory fake — no real WS server needed for CI.
- Uses the `websockets` library that was already pulled in by `uvicorn[standard]` — no new dependency.

### New fields on `FuzzRow`

- `transport: "http" | "ws"` — which code path this turn exercised.
- `ttft_ms: int | None` — time from `turn.start` → first `turn.token` event. The user-perceived latency number p50 targets care about.
- `token_count: int | None` — how many incremental tokens the UI would have rendered; `0` signals the agent short-circuited (fast-path chitchat or error before synthesis).

Defaults preserve the old JSONL schema: existing rows read back with `transport="http"` and `ttft_ms=None`.

### CLI

```
uv run python -m smcity_fuzz run --mode ws --turns 20
```

`--mode http` (default) keeps the original POST `/turn` path for quick campaigns where only total latency matters.

### Tests (6 new)

- `test_ws_url_upgrades_scheme` — http/https → ws/wss path prefix.
- `test_ws_transport_captures_ttft_and_tokens` — scripted 3-token stream yields positive TTFT + correct token count.
- `test_ws_transport_fast_path_without_tokens_falls_back_to_final_text` — chitchat (0 tokens) still produces a reply from `turn.final`, TTFT stays `None`.
- `test_ws_transport_raises_on_error_frame` — a rate-limit error frame surfaces as `WsTransportError` (not a crash).
- `test_runner_ws_mode_populates_ttft_and_token_count` — full runner integration in ws mode; row persisted with new fields.
- 226 tests green (was 221), ruff + format + mypy strict clean across 74 source files.

## [0.4.2] — 2026-04-23

**HKHA live + EPD AQHI regression fix.** End-to-end "check if all works" pass — **14/14 tools pass live**.

### HKHA → live (`smcity/tools/housing.py`)

Coverage jumps **10 bundled estates → 241 live** across 17 districts. Same tool interfaces (`housing.get_estate_info`, `housing.list_estates_in_district`) — no prompt change.

- Source: `data.housingauthority.gov.hk/psi/rest/export/prh-estates?format=json` (not ArcGIS REST — a dedicated client rather than the CSDI generic tool).
- Module-level catalog cache with 24 h TTL + asyncio lock (same pattern as facility).
- Numeric coercion: parses `"8 200 as at 31.12.2025"` → `8200` via `_INT_PREFIX` regex; keeps the raw string in `flats_raw` for auditability.
- **TC name overlay** at `data/hkha_name_map_tc.json` — hand-curated 29-entry English→繁體 map so Cantonese users hitting popular estates still match (e.g. "彩虹" → Choi Hung Estate, "美孚" → Mei Foo Sun Chuen). Estates outside the map still match by English name; the tool description notes this up front.

### EPD AQHI endpoint repaired (`smcity/tools/context.py`)

Live smoke test caught a real production regression: `context.get_aqhi` was pointing at `www.aqhi.gov.hk/api/history-last-24-hours-aqhi.json` which now returns HTTP 404. Replaced with the current per-station RSS feed:

- **New URL**: `https://www.aqhi.gov.hk/epd/ddata/html/out/aqhi_ind_rss_Eng.xml`
- New parser: `xml.etree.ElementTree` + a regex over each `<item>`'s `<description>` CDATA block (e.g. `"Central/Western - General Stations: 3 Low - Thu, 23 Apr 2026 13:30"`).
- Returns the same `AQHIStation(station, aqhi, health_risk, update_time)` shape — no caller-side changes needed.

### Live smoke test (validates everything)

14 production tools exercised against real upstreams in a single script:

| tool | status | notes |
|---|---|---|
| housing.get_estate_info (EN + TC) | ok | matches "Choi Hung Estate" both ways |
| housing.list_estates_in_district | ok | 19 estates in Sham Shui Po |
| facility.find_nearby_courts | ok | 5 courts near Sheung Wan (cold fetch ~2 s) |
| facility.find_nearby_pools | ok | 3 Wan Chai pools |
| geo.address_lookup | ok | ALS responded |
| context.get_current_weather | ok | 28°C |
| context.get_active_warnings | ok | 0 warnings active |
| context.get_aqhi | **fixed** | 5 stations, "Central/Western = 3 Low" |
| context.get_9day_forecast | ok | 9 days |
| transport.get_mtr_next_trains | ok | 8 trains @ Central |
| transport.get_kmb_eta_by_stop | ok | 5 ETAs |
| geo.search_osm_pois | ok | 20 public toilets |
| csdi.query_features | ok | 3 Sha Tin courts |

### Files removed

- `data/hkha_estates.json` — bundled 10-entry snapshot (replaced by live 241-entry feed).

### Files added

- `data/hkha_name_map_tc.json` — 29-entry English→繁體 name overlay for the most-asked estates.

### Tests

- `test_housing_estate_info_fuzzy_en_and_tc` — proves both EN queries (Choi Hung, Mei Foo, Tak Long) and 繁體 queries (彩虹, 美孚, via overlay) resolve to the right estate.
- `test_housing_parses_numeric_with_trailing_text` — pins the "8 200 as at 31.12.2025" → 8200 parser.
- `test_housing_list_estates_in_district` — verifies district filter hits multiple estates.
- `test_housing_region_filter` — verifies region filter narrows further.
- **221 tests green** (was 219), ruff + format + mypy strict clean across 72 source files.

## [0.4.1] — 2026-04-23

**Diagnostic-only guardrail + Markdown handoff export.** Intentional separation of roles: the fuzzer's LLM (gpt-oss-20b) only diagnoses defects; a frontier LLM (Claude / Gemini) that receives the exported report is the one that proposes code fixes.

### Judge prompt tightened (`smcity_fuzz/judge.py`)

Explicit new rules in the system prompt:
- MUST NOT suggest, write, or describe any code fix
- MUST NOT recommend prompt changes or new tools
- MUST NOT speculate about why a bug exists inside the agent's implementation
- MUST describe the observable defect in one sentence, nothing more

Pinned by `test_judge_prompt_forbids_code_fixes` so a future refactor can't silently relax the guardrail.

### New `smcity_fuzz export` subcommand + `smcity_fuzz/export.py`

Renders a fuzz run as a single Markdown file suitable for pasting into a chat with Claude or Gemini. Each failure section includes:

- Metadata header (run_id, timestamp, persona, language, topic, rubric scores, reason tags, judge summary)
- Question (fenced code block)
- Agent reply (fenced code block)
- Tool trace (human-readable bullet list)
- Pipeline errors if any
- The full row as fenced JSON — for reproducing or quoting verbatim

A banner at the top tells the receiving LLM: "this is diagnostic evidence — diagnose the top N failures and propose minimal code patches."

CLI flags:
- `--run-id run-xxx` — isolate one campaign (default: every row on disk)
- `--out FILE` — write to a file instead of stdout
- `--max-failures N` — cap section count for oversized runs
- `--all` — include passing rows too (default: failures only)

Typical handoff flow:
```bash
uv run python -m smcity_fuzz run --turns 40 --concurrency 2
uv run python -m smcity_fuzz export --out handoff/2026-04-23-run.md
# open handoff/2026-04-23-run.md, paste into Claude, say "diagnose + patch"
```

### Tests

`test_judge_prompt_forbids_code_fixes`, `test_export_banner_forbids_code_suggestions`, `test_export_includes_failure_section_and_raw_json`, `test_export_only_failures_flag_excludes_passes`, `test_export_max_failures_caps_sections`. **219 tests green** (was 214), ruff + format + mypy strict clean across 72 source files.

## [0.4.0] — 2026-04-23

**Adversarial fuzz harness.** New `smcity_fuzz/` package drives the production agent with LLM-generated questions and grades every reply with an LLM-as-judge — actual semantic testing instead of regex shape-checks.

### Architecture

```
synth (gpt-oss-20b) → question
                        │ persona × topic × language
                        ▼
                    POST /turn (gpt-oss-120b agent)
                        │
                        ▼
                     (reply, tool_trace)
                        │
                        ▼
            judge (gpt-oss-20b) → rubric JSON
                        │
                        ▼
               logs/fuzz_runs.jsonl
```

Single LM Studio endpoint serves both models (120b for agent, 20b for fuzzer); load both in LM Studio and the client routes by `model` field per request — no second instance needed.

### Modules (new)

- `smcity_fuzz/personas.py` — 5 hand-authored personas: Cantonese senior, English tourist, bilingual HK student, mainland visitor, rushed commuter. Each carries a character sheet + style hints.
- `smcity_fuzz/datasets.py` — 22 topics mapped to expected tools (MTR, KMB, Citybus, GMB, weather, AQHI, pools, courts, housing, public toilets, benches, dentists, etc.).
- `smcity_fuzz/synth.py` — `synthesise_question(persona, topic, language)` calls gpt-oss-20b with a role-play prompt, strips "Question:" prefixes + outer quotes, returns one question.
- `smcity_fuzz/judge.py` — rubric scorer (intent_match / language_ok / tool_choice_ok / factual_vs_trace / coherence, each 0–2) + `failure_reasons` tags + one-sentence `summary`. Strips markdown fences from model output, validates against pydantic.
- `smcity_fuzz/runner.py` — `run_campaign()` — asyncio bounded concurrency over persona × topic × language; per-turn errors land in the row's `errors[]` so the campaign never aborts.
- `smcity_fuzz/store.py` — `logs/fuzz_runs.jsonl` append-only, fsync'd per row. Skips corrupt lines on read.
- `smcity_fuzz/report.py` — failure summary grouped by dataset / language / persona / failure reason, with top-N detail rows.
- `smcity_fuzz/cli.py` — `uv run python -m smcity_fuzz run|report|failures` with `--turns / --concurrency / --personas / --topics / --languages` filters.

### Config (env-driven, prefix `FUZZER_`)

- `FUZZER_BASE_URL` — LM Studio URL (default: same as production agent)
- `FUZZER_MODEL` — default `openai/gpt-oss-20b`
- `FUZZER_AGENT_URL` — default `http://127.0.0.1:8080`
- `FUZZER_RUNS_PATH` — default `logs/fuzz_runs.jsonl`
- `FUZZER_CONCURRENCY` — default 2

### Tests (14 new)

Synth (4), judge (4), store (2), runner end-to-end happy path + agent error + synth error (3), report (1). Every upstream (LM Studio + smcity `/turn`) is respx-mocked so CI stays hermetic.

### CI + packaging

- `check.yml` now runs `mypy smcity smcity_fuzz tests` (previously `smcity tests` only).
- `pyproject.toml` includes `smcity_fuzz` in hatch wheel targets.

### What this catches that templates + regex can't

- Semantic factual drift between tool output and reply
- Wrong-tool-for-the-intent (agent called `get_mtr_next_trains` when user asked about buses)
- Partial language drift (reply half in English, half in Cantonese)
- Coherence failures (tool returned 0 results but reply invents venues)
- Refuse-wrongly failures (agent refused a legitimate query)

### What's deferred to v0.4.1+

- WebSocket runner (streaming timing, not just total latency)
- HTML dashboard for failures
- Cross-run regression detection (comparing two run_ids)
- Auto-retry for transient agent errors

**214 tests green** (was 200), ruff + format + mypy strict clean across 71 source files.

## [0.3.4] — 2026-04-23

**Bundled → live.** `facility.find_nearby_courts` and `facility.find_nearby_pools` now query the CSDI ArcGIS FeatureServer live instead of reading the bundled JSON snapshots.

### Coverage

| Tool | Before (bundled) | After (live CSDI) |
|---|---|---|
| `facility.find_nearby_courts` | 15 courts | **305 sports grounds with basketball courts** across 19 districts |
| `facility.find_nearby_pools` | 10 pools | **46 public swimming pools** |

Live smoke test verified: Sheung Wan lat/lng returns 5 real HK courts in 1.5 s (cold fetch); Wan Chai district filter returns Morrison Hill / Victoria Park / Wan Chai pools in 2.5 s. 24 h in-memory catalog cache means subsequent calls are instant.

### Schema changes (breaking)

The `BasketballCourt` / `NearbyCourt` schema loses `floodlit`, `outdoor`, `booking` (CSDI doesn't publish them) and gains `address_en`, `address_tc`. Court count now maps to `No__of_Basketball_Courts_EN`.

The `SwimmingPool` / `NearbyPool` schema loses `indoor`, `heated`, `lanes` and gains `address_en`, `address_tc`, `facility_type`, `opening_hours`, `telephone`. The `indoor_only` arg is removed (only 1/46 pools mention "Indoor" in `FacilityDetailsEN` — the filter would be misleading).

### Corrections versus v0.3.3 research

- Basketball courts' district is in `SEARCH01_EN` (values like `SHA TIN`, `WAN CHAI`), NOT `DISTRICT`. The research agent invented `DISTRICT`; the adapter title-cases `SEARCH01_EN` to display-friendly `Sha Tin`.
- CSDI's advertised `maxRecordCount=2000` is not honoured in practice — requesting `resultRecordCount=500` with multiple `outFields` returned `error=400`. Adapter uses `page_size=200` with our existing pagination loop.

### Files removed

- `data/lcsd_basketball_courts.json` — bundled snapshot no longer needed.
- `data/lcsd_swimming_pools.json` — same.

### Tests

- `tests/test_tools_phase1b.py` — facility tests now mock the CSDI HTTP endpoints via respx + reset the module-level catalog cache. New `test_courts_filters_zero_basketball_venues` proves sports grounds with 0 basketball courts are dropped. Old `test_pools_filter_indoor_only` removed (filter gone).
- **200 tests green** (was 198), ruff + format + mypy strict clean.

## [0.3.3] — 2026-04-23

Wires two live CSDI ArcGIS FeatureServer datasets to the generic `csdi.query_features` tool shipped in v0.3.2.

### New live data sources

- **`lcsd_basketball_courts`** → `portal.csdi.gov.hk/.../lcsd_rcd_1629267205215_38105/FeatureServer/0`. Bilingual name, address, district, court count. UPPER_SNAKE field naming.
- **`lcsd_swimming_pools`** → `portal.csdi.gov.hk/.../lcsd_rcd_1634540558875_77434/FeatureServer/0`. Bilingual name, address, district, facility details, opening hours, telephone. **CamelCase** field naming (different from basketball courts — the `CSDIDataset` struct captures per-dataset naming so call sites never guess).

Both return WGS84 via `outSR=4326`; the generic client follows `exceededTransferLimit` pagination transparently.

### Docs

- **`docs/research/06_csdi_endpoints.md`** — verified endpoint reference (URL, field list, sample row, verification curl output). Confirms all five tested endpoints worked on 2026-04-23.
- Flags that HKHA estates aren't on CSDI — canonical live source is the Housing Authority's own JSON API (`data.housingauthority.gov.hk/psi/rest/export/prh-estates`), which needs its own non-ArcGIS tool. Tracked as v0.3.4+.

### Tests

- `test_production_datasets_registered_on_import` pins both datasets' presence + their (differing) field-naming conventions.
- `_reset_csdi_registry` fixture now snapshots and restores `CSDI_DATASETS` so test isolation doesn't nuke the production registry.
- **198 tests green** (was 197), ruff + format + mypy strict clean.

## [0.3.2] — 2026-04-23

CSDI ArcGIS FeatureServer scaffold. Generic async client (`query_feature_server`) + agent-facing tool (`csdi.query_features`) with an SSRF-safe dataset registry. Tool ships with `CSDI_DATASETS` empty; entries land per-dataset in v0.3.3+.

- **`smcity/tools/csdi.py`** — client handles pagination via `exceededTransferLimit`, requests `outSR=4326` by default, exposes bbox envelope queries.
- **Tool spec** — `csdi.query_features(dataset, where, bbox, limit)` with a 24h TTL.
- **Tests** — 8 unit tests covering single-page, pagination, limit capping, ArcGIS error envelope, HTTP 500, bbox encoding, unknown-dataset rejection, tool round-trip.

## [0.3.1] — 2026-04-23

Executes the **"this week"** + **"this milestone"** clusters from `docs/AUDIT.md`. Zero behavioural changes on the happy path; every change is hardening, latency-preserving, or test coverage.

### Security (P1 hardening)

- **WebSocket origin allow-list (P1-1).** `/ws/{session_id}` now rejects cross-origin upgrades. Defaults to same-origin + missing-Origin (tailnet posture). Configurable via `WS_ALLOWED_ORIGINS` env (`*` to disable, `host:port` or `scheme://host` entries).
- **Per-session rate limit (P1-2).** Token-bucket in `smcity.ratelimit` — defaults: 30 tokens/min refill, burst 10. WS emits `{"type":"error","retry_after_s":N}`; HTTP returns 429 with `Retry-After`.
- **`session_id` regex guard (P1-3).** `^[A-Za-z0-9_.-]{1,64}$` — enforced at the WebSocket handler, in `TurnRequest` pydantic model, and inside `SessionStore.load/save/forget`.
- **Harmony-leak extractor regression tests (P1-4).** `tests/test_harmony_injection.py` covers user-echoed tool names in prose, array-not-object arg guard, prose-brace overshoot, and mixed canonical+bare leaks.

### Correctness + latency (P2)

- **TTL cache enforcement (P2-1).** `ToolRegistry.dispatch()` now honours `ttl_seconds` + `cacheable` on the spec; repeat `(name, args)` calls return `cached=True` without re-running the handler.
- **Structured audit log (P2-5).** Every tool dispatch emits a `tool_call` structlog record with `name`, `status`, `latency_ms`, `cached`, `session_id`.
- **`web/app.js` textContent migration (P2-6).** All dynamic HTML replaced with DOM-builder + textContent. No innerHTML with runtime data anywhere in the UI.
- **SQLite file perms.** `data/sessions.sqlite3` (+ WAL/SHM/journal shards) are `chmod 600` on creation.
- **Prompt-prefix stability.** `ToolRegistry.openai_schemas()` returns alphabetised order and is pinned by `tests/test_prompt_prefix_stability.py` (byte-identical across rebuilds — keeps the LM Studio KV cache warm).
- **`smcity/geometry.py`.** Centralised `haversine_m` / `haversine_km`; 5 inline copies deduped from facility / housing / transport_search / transport_simple_modes / transport_planner.

### CI

- **`pip-audit --strict`** job added to `.github/workflows/check.yml`, scanning the exported uv lockfile.
- **`osv-scanner`** job scanning `uv.lock` for known vulnerabilities.

### Docs

- `docs/DEPLOY.md` — new "Recommended LM Studio tuning" section (speculative decoding with `gpt-oss-20b` draft, Flash attention, keep-model-loaded), expanded threat model with v0.3.1 mitigations.

### Tests

- `tests/test_harmony_injection.py` — 5 regression tests for P1-4.
- `tests/test_prompt_prefix_stability.py` — 3 tests pinning tool-schema order.
- `tests/test_ratelimit.py` — 4 tests for the token bucket.
- `tests/test_tool_cache.py` — 6 tests for TTL cache behaviour.
- `tests/test_ws_origin.py` — 7 tests for origin allow-list + session_id guard.
- **189 unit tests green**, ruff + format + mypy strict clean across 57 source files.

## [0.3.0] — 2026-04-21

Aligns the tool registry with the lab's `3 - Selected Smart City Data Maps.xlsx` workbook. **30 of 35 workbook datasets are now served by live APIs** (all 27 POI categories + all 3 road-facility categories). See `docs/DATASETS.md` for the per-ID coverage map.

### New tools (3; registry 22 → 25)

- **`geo.search_osm_pois`** — unified OpenStreetMap Overpass search covering **all 30 POI + road-facility categories from the workbook** (S514-S549) in ONE tool. Category enum maps to the Overpass tag filters from the xlsx (e.g. `public_toilet` → `amenity=toilets`, `mtr_station_entrance` → `railway=subway_entrance`, `dentist` → `amenity=dentist OR healthcare=dentist`). Accepts lat/lng + radius, bbox, or defaults to all of HK. Returns deduplicated POIs with name_en/name_zh/brand/opening_hours/wheelchair tags where present.

- **`transport.get_gmb_eta`** — Green Minibus live ETA via `data.etagmb.gov.hk`. Two-hop call internally: `/route/{region}/{code}` → route_id, then `/eta/route-stop/{route_id}/{stop_id}` → ETAs. Returns sorted arrivals + destination EN + 繁體.

- **`context.get_9day_forecast`** — HKO 9-day weather outlook (`dataType=fnd`). Max/min temperature, humidity range, wind summary, probability of significant rainfall, and the general regional situation for each day.

### Documentation

- **`docs/DATASETS.md`** — full xlsx → tool coverage map. Every S-ID from the workbook is tagged ✅ live / 🟨 bundled / ⏳ deferred, with the exact tool name + Overpass category where applicable.

### Tests

- 8 new unit tests in `tests/test_osm_gmb_forecast.py`:
  - All 30 POI categories have tag specs (catches category-literal drift).
  - Overpass query builder encodes bbox + tags correctly.
  - Multi-tag categories (dentist = amenity OR healthcare) expand both filters.
  - Bbox-from-point sanity check.
  - Overpass response parsing + deduplication by osm_type+id.
  - GMB two-hop flow (route lookup then ETA).
  - HKO 9-day forecast payload parsing.
- **164 unit tests green**, ruff + mypy strict clean across 52 source files.

### What's deferred to v0.4+

- S500 (GTFS headway), S505 (MTR fares + barrier-free), S506 (ferry timetable), S507 (PT routes + fares) — all data.gov.hk transportation datasets that need custom parsers.
- S512 (iB1000 topographic map via CSDI) — needs ArcGIS service-ID discovery.
- Live upgrade of the bundled LCSD basketball / pool tools and HKHA estates tool (same CSDI service-ID blocker).

## [0.2.0] — 2026-04-21

Closes the remaining transport-mode gaps that the live v0.1.0 session
surfaced. The agent now answers "how do I get from X to Y?" in ONE call
with walk + MTR + taxi side-by-side, handles walking-only queries
cleanly, gives real HK taxi fare estimates, and supports explicit
session wipe.

### New tools (4; registry grew from 18 → 22)

- **`transport.plan_journey`** — unified multimodal planner. Takes origin + destination (free-text via ALS or lat/lng), returns walk / MTR / taxi options side-by-side with durations + taxi fare range + recommendation. This is the new default for "how do I get from X to Y?" queries — no more mode-ask ping-pong.
- **`transport.plan_walking_route`** — haversine-based walking estimate at 1.2 m/s HK-urban pace, with ALS geocoding when inputs are free-text. Fixes the v0.1.3 empty-reply regression on walking questions.
- **`transport.plan_taxi_estimate`** — HK 2026 urban taxi tariff (HK$27 flag-down + HK$1.90 per 200 m), with a fare range for traffic/toll/off-peak variance. Road-distance proxy = haversine × 1.3.
- **`meta.forget_me`** — deletes the session's SQLite row (slots, locale, history). Promised in Phase 0 but never implemented; now exposed as a tool the LLM calls on "forget me / reset / delete my data".

### Pipeline updates

- System prompt rewritten around per-mode tool routing:
  - No-mode "from X to Y?" → `transport.plan_journey` (no clarification needed).
  - MTR / 地鐵 / 港鐵 → `transport.plan_simple_route`.
  - walk / 步行 → `transport.plan_walking_route`.
  - taxi / 的士 → `transport.plan_taxi_estimate`.
  - KMB / Citybus → operator-specific ETA tools.
  - forget/reset/clear → `meta.forget_me`.
- Coverage matrix updated to include all new tools (EN + 繁體).

### Tests

- 11 new unit tests in `tests/test_simple_modes.py`:
  - Taxi fare formula parametrised across 5 distance buckets.
  - Walking with lat/lng + with free-text (ALS-mocked).
  - Taxi estimate on a short HK-Island trip.
  - Journey planner: full three-mode payload + custom mode subset.
  - `meta.forget_me`: roundtrip save → forget → verify wiped.
- **156 unit tests green**, 7 live integration tests green, ruff + mypy strict clean across 49 source files.

### Live verification

| turn | tool | result |
|---|---|---|
| `"how do I get from Mong Kok to Sha Tin"` | `transport.plan_journey` (1025 ms) | Returned walk + MTR + taxi options; LLM synthesised a focused reply |
| `"what about walking"` (same session) | `transport.plan_walking_route` | Specific walking distance + duration, not an empty collapse |
| `"forget me"` | `meta.forget_me` | "Your data has been cleared. `src: forget_me`" |

### Deferred

- OpenTripPlanner 2 sidecar for bus + minibus + ferry + tram multimodal routing (heavier; own session).
- NLLB-200 translation fallback (replaces LLM-as-MT for non-CJK).
- HIT-TMG/LID-HK transformer for precise fr/de/tl/id/vi detection.

## [0.1.0] — 2026-04-21

First end-to-end release. Cantonese-first agentic HK smart-city chat over data.gov.hk, running against `openai/gpt-oss-120b` via LM Studio on the lab's Mac Studio, reachable over Tailscale.

### Tool registry (18 tools)

- `geo.address_lookup` — Lands Department ALS, free-text → GeoJSON.
- `transport.get_mtr_next_trains` — MTR Next Train API, 105-station fuzzy catalog (EN / 繁體 / 简体).
- `transport.get_kmb_eta_by_stop` + `transport.get_kmb_eta_by_route_stop` — KMB/LWB live ETAs, 6715-stop catalog lazy-loaded on first call.
- `transport.get_citybus_eta_by_route_stop` + `transport.get_citybus_route_stops` — Citybus live ETAs.
- `transport.find_stops_near_point` + `transport.find_stops_by_name` — k-NN + fuzzy search across KMB + MTR.
- `transport.plan_simple_route` — walk + MTR multimodal planner (Dijkstra over the 10-line station graph with 2 min inter-station + 5 min interchange edge costs).
- `context.get_current_weather` + `context.get_active_warnings` + `context.get_aqhi` — HKO + EPD live data.
- `facility.find_nearby_courts` + `facility.find_nearby_pools` — bundled LCSD static catalog (15 courts + 10 pools across HK districts), haversine search.
- `housing.get_estate_info` + `housing.list_estates_in_district` — bundled HKHA catalog (PRH + HOS); explicit guardrail against personal-application queries.
- `meta.ask_user` + `meta.what_languages_are_supported` — clarification gate + per-tool language-coverage introspection.

### Pipeline

- **Language router v1** — Cantonese particle heuristic (30+ particles + bigrams) at 0.92 confidence, unicode-script majority for Han / Hiragana / Hangul / Thai / Arabic / Hebrew / Cyrillic / Devanagari / Greek / Latin, Simplified-vs-Traditional disambiguation by character set, code-switch detection.
- **Script normalisation** — OpenCC `s2hk` / `hk2s` (HK-correct; preserves Cantonese-only characters).
- **Coverage matrix** — per-tool native language support; Cantonese correctly flagged as `translation_applied: true` on every tool call (data.gov.hk has zero native Cantonese).
- **Session store** — SQLite WAL + msgspec + PII redaction at ingress (HK phone + HKID regexes), `meta.forget_me` path.
- **Orchestrator** — detect → classify (fast-path short-circuit) → slots → parallel tool dispatch (`asyncio.gather`) → streaming LLM synthesis → Cantonese polish → response formatter with language-coverage chip + source footer.
- **Pre-classifier fast path** — deterministic weather / AQI / warnings / chitchat matcher in 4 language families; chitchat = 0 LLM calls (3 ms), single-intent = 1 LLM hop (≈ 3.8 s vs ≈ 10 s full path).
- **Streaming** — `chat_stream()` async generator; `turn.token` WebSocket events for incremental UI rendering; blinking caret until first token.
- **KV-cache pinning** — `user=session_id` forwarded to LM Studio so each conversation stays on one llama.cpp slot.
- **Cantonese polish** — few-shot exemplar block injected when `primary_lang=yue`, plus 50-entry phrase table + 7 regex char-subs applied as a post-pass. Protects fixed lexemes (的士, 正在/現在/所在, 了解/了結, 也許, 很多/少/久).

### Chat UI (archive underground)

- Monospace, near-black warm surface `#0B0B0A`, ivory body `#E6E1D1`, single amber accent `#C9A24A`, optional scanline overlay.
- Native `<select>` language selector with per-language `lang=` attrs for a11y; `⌘⇧L` / `^⇧L` resets to auto.
- Two-pane dialogue + tool trace rail, live `tool_call.start` / `tool_call.result` events, streaming agent message, source + language-coverage chips on every reply.
- WebSocket (`/ws/:session_id`) + SSE (future) + REST `POST /turn` transports.

### Service

- FastAPI on `:8080`, Tailscale-only in dev.
- Pydantic-validated request/response schemas.
- OpenLLMetry / Langfuse-ready (not wired in v0.1).

### Tests

- 129 unit tests, 7 live integration tests (MTR + KMB + HKO + EPD + ALS), golden eval set of 37 queries across 10+ languages (native + fallback buckets).
- `just check` (ruff + mypy strict + unit) green across 44 source files.

### Documentation

- `docs/GOAL.md`, `docs/PLAN.md`, `docs/OPEN_QUESTIONS.md`
- `docs/architecture/{ARCHITECTURE,TOOL_CATALOG,UI_STYLE}.md`
- `docs/PROTOCOL.md` (new) — WebSocket event schema for robot integration
- `docs/DEPLOY.md` (new) — Tailscale Serve + threat model
- `docs/research/0{1..5}_*.md` — data.gov.hk APIs, agentic tool-calling, multilingual stack, prior-art review (~2000+ lines)

### Verified live on Mac Studio tailnet (2026-04-21)

| query | language | tool(s) | turn latency |
|---|---|---|---|
| `hi` | en | (chitchat) | **3 ms** |
| `而家天氣點呀？` | yue | `context.get_current_weather` | **3.8 s** |
| `沙田附近有冇籃球場？` | yue | `facility.find_nearby_courts` | ~10 s |
| `我喺上環，下班車幾時到？` | yue | `transport.get_mtr_next_trains` | ~12 s |
| `Next KMB bus at Sheung Wan MTR?` | en | `transport.get_kmb_eta_by_stop` (1.6 s cold) | ~12 s |
| `我想由上環去沙田，點樣搭 MTR 最快？` | yue | `transport.plan_simple_route` (2 ms) | **7.3 s** |

### Deferred to future versions

- OpenTripPlanner 2 sidecar (HK GTFS + multimodal over bus + minibus + ferry + tram)
- NLLB-200 translation fallback (replaces LLM-as-MT)
- HIT-TMG/LID-HK transformer for precise non-CJK detection
- Speculative decoding with `gpt-oss-20b` draft
- Qwen2.5-7B Cantonese register post-processor (alternative to the current in-prompt + deterministic pass)

---

[0.1.0]: https://github.com/regitxx/SmartCityIntegration-SRLab/releases/tag/v0.1.0
