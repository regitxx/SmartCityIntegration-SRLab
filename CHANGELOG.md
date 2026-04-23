# Changelog

All notable changes to this project are documented here. Versions follow [SemVer](https://semver.org/).

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
