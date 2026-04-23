# Changelog

All notable changes to this project are documented here. Versions follow [SemVer](https://semver.org/).

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
