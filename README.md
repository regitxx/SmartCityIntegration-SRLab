# SmartCityIntegration — HK Lab of Social Robotics

Agentic chat system that answers Hong Kong smart-city questions — transport (MTR / KMB / Citybus / minibus / walking / taxi / multimodal), public facilities (LCSD basketball courts + pools), public housing (HKHA estates), weather / air quality / warnings — by calling [data.gov.hk](https://data.gov.hk/en/), CSDI, and other HKSAR open-data APIs through a strict, typed tool registry.

Cantonese-first (廣東話), 100 % language coverage from v0 via a translation-fallback layer. Clean seams for robot / voice integration (WebSocket streaming + stable `/turn` REST).

## Status — **v0.5.3** · 2026-05-24

- **55 live tools.** Every data-producing tool hits a real upstream; the 3 formerly-bundled tools (LCSD courts, LCSD pools, HKHA estates) all migrated to live feeds in v0.3.4 / v0.4.2; v0.5.0 split the POI mega-tool into 30 per-category thin tools (`geo.find_dentist`, `geo.find_bench`, …).
- **338 unit tests green** (4 new orchestrator integration tests + 65 unit tests across the v0.5.x engines). Ruff + format + mypy strict clean across all 39 source files.
- **Three-stage orchestrator guard rails (v0.5.1)** — declarative engines run at each LLM-turn lifecycle stage: `tool_call_gates` (pre-execution) reject ill-shaped tool-call proposals; `chain_rules` (post-execution) auto-complete known tool chains deterministically; `synthesis_invariants` (post-synthesis) reject replies that deny non-empty tool data across 13 supported languages. See [docs/architecture/ARCHITECTURE.md §3.7](docs/architecture/ARCHITECTURE.md).
- **Tool scope tags (v0.5.1)** — `ToolScope` enum + `domain` field on every `ToolSpec` auto-prepend `[DEFAULT: …]` / `[SPECIALIZED: …]` / `[FALLBACK]` markers into descriptions. 11 transit + meta tools tagged where confusion was documented.
- **Adversarial LLM fuzz harness** (`smcity_fuzz/`, v0.4.0+; v0.5.0 contracts-based judge) — synthetic-user agent drives the real agent, contract functions in `smcity_fuzz/contracts.py` grade outcomes; exports paste-ready Markdown reports for Claude / Gemini handoff.
- **OpenTripPlanner 2 sidecar** (`otp/`, v0.4.4) — true multimodal (walk + bus + MTR + minibus + ferry) routing. Tool wired; activation requires Docker + HK GTFS graph build (see `otp/README.md`).

LLM backbone: `openai/gpt-oss-120b` served by LM Studio on the lab's Mac Studio (`earnests-mac-studio.taila366aa.ts.net:1234`) over Tailscale. OpenAI-compatible; native tool-calling + streaming verified. For the fuzzer, pair with `openai/gpt-oss-20b` loaded alongside in the same LM Studio instance.

## Try it

Prereqs:
- macOS or Linux
- [`uv`](https://github.com/astral-sh/uv) — `brew install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Optional: [`just`](https://github.com/casey/just) — `brew install just`
- Tailscale connected and on the **Earnest Design Lab** tailnet so the Mac Studio endpoint resolves (or set `LLM_BASE_URL` to your own LM Studio)

```bash
cp .env.example .env                      # adjust if needed
uv sync --extra dev                       # or: just install
uv run python -m scripts.llm_ping         # smoke-test LM Studio
uv run pytest -q -m "not integration"     # 338 unit tests
uv run uvicorn smcity.app:app --host 0.0.0.0 --port 8080
# open http://localhost:8080
```

All checks: `just check` (ruff + mypy + unit tests). Integration suite (live data.gov.hk + LM Studio): `just integration`.

## Demos

**Cantonese MTR routing:**
```
> 我想由上環去沙田，點樣搭 MTR 最快？

上環搭港島綫往金鐘，約 4 分鐘到金鐘。
喺金鐘轉東鐵綫直達沙田，大約再坐 12 分鐘。
全程大約 21 分鐘，係最快嘅路線。

src: plan_simple_route
```

**Cantonese basketball query (live CSDI):**
```
> 沙田附近有冇籃球場？

沙田區有 7 個 LCSD 籃球場，包括沙田運動場（3 個場）、
沙田賽馬會泳池籃球場、源禾路遊樂場等。想睇邊個最近你？

src: find_nearby_courts
```

**HKHA estate lookup (live Housing Authority API):**
```
> 彩虹邨有幾多樓？

彩虹邨喺黃大仙區，1962 年入伙，一共有 11 座，提供 7 455 個單位。
單位面積由 14 到 40 平方米。

src: get_estate_info
```

**Fast-path weather (~3.8 s, one LLM hop, no first hop):**
```
> 而家天氣點呀？

依家香港係 27 度，濕度 76 %，過去一個鐘冇雨，紫外線指數好低（0.4）。

src: get_current_weather
```

**Chitchat (3 ms, zero LLM calls):**
```
> hi
Hi — ask me about HK transport, weather, air quality, courts, pools, or housing.
```

## Adversarial fuzz harness (`smcity_fuzz/`)

Discover accuracy bugs the test suite can't catch — wrong-tool-for-intent, factual drift vs tool output, partial language drift, wrongful refusals, hallucinated venues.

```bash
# Load gpt-oss-20b alongside 120b in LM Studio once, then:
uv run python -m smcity_fuzz run --mode ws --turns 40 --concurrency 2

# Export paste-ready Markdown for Claude / Gemini to diagnose:
uv run python -m smcity_fuzz export --out handoff/2026-04-23-run.md

# Or inspect past failures in the terminal:
uv run python -m smcity_fuzz failures --top 20
```

The fuzzer's judge is diagnostic-only by design — it describes defects, never proposes fixes. A frontier LLM (or a human) reviewing the exported Markdown decides the patches. See [docs/ACCURACY_REVIEW.md](docs/ACCURACY_REVIEW.md) for the full list of failure modes the judge is tuned to detect.

## Multimodal planning (`otp/`)

When you want true bus + MTR + minibus + ferry journeys:

```bash
cd otp/
# (one-off) drop HK GTFS + OSM PBF in ./data/, then:
docker compose run --rm otp2 --build --save    # ~15–30 min
docker compose up -d
# transport.plan_multimodal_journey picks it up automatically.
```

Full walkthrough in [otp/README.md](otp/README.md). The hand-rolled Dijkstra MTR planner (`transport.plan_simple_route`) remains as the fast path + fallback when the sidecar is offline.

## Layout

```
smcity/                       55-tool agent
├── app.py                    FastAPI — /health, /turn, /ws/:session_id
├── orchestrator.py           per-turn pipeline: detect → classify → tools → stream
├── tool_call_gates.py        v0.5.1 — pre-execution gate engine (ASK_USER_ONLY_GATE)
├── chain_rules.py            v0.5.1 — post-execution chain engine (POI_CHAIN_RULE)
├── synthesis_invariants.py   v0.5.1 — post-synthesis invariant engine (DATA_DENIAL)
├── prompts.py                system prompt + Cantonese few-shot exemplars
├── cantonese_polish.py       formal→colloquial post-pass (60+ subs, 7 regex rules)
├── classifier.py             deterministic fast-path (weather/aqi/warnings/chitchat)
├── ratelimit.py              per-session token bucket (v0.3.1)
├── langrouter/               language detection + coverage matrix
├── tools/                    13 transport + 4 context + 2 facility + 2 housing
│                             + 32 geo (incl. 30 OSM POI per-category tools) +
│                             1 csdi + 3 meta = 55 tools.
│                             ToolSpec carries scope/domain (v0.5.1) for the
│                             [DEFAULT: …] / [SPECIALIZED: …] / [FALLBACK] markers.
└── …                          see docs/architecture/TOOL_CATALOG.md for details

smcity_fuzz/                  adversarial fuzz harness (v0.4.0+; v0.5.0 contracts)
├── contracts.py              v0.5.0 — code-based verdict per dataset (the judge)
├── coverage_gen.py           multilingual question generator (en/yue/zh-Hant/zh-Hans)
├── coverage_run.py           async POST /turn runner, locale_override per row
├── coverage_report.py        Markdown + JSON aggregator over contracts.evaluate
└── cli.py                    `python -m smcity_fuzz coverage {generate,run,report}`

otp/                          OpenTripPlanner 2 sidecar (v0.4.4)
├── docker-compose.yml        pinned 2.6.0, loopback :8080, 4 GB heap
└── README.md                 GTFS sources, graph build, smoke test

web/                          archive-underground UI (vanilla JS + WebSocket)
data/                         static reference data — MTR stations + lines only;
                              HKHA name_tc overlay is the one remaining
                              hand-curated file, all other data is live
scripts/                      llm_ping.py, live_smoke.py, coverage_v2.sh, rejudge_v1.sh
tests/                        338 unit tests; one integration suite per engine
docs/                         GOAL / PLAN / DEPLOY / ACCURACY_REVIEW / audit /
                              architecture / research
```

## Feature inventory (v0.5.3)

**Tool registry — 55 tools**

| domain | tools |
|---|---|
| `transport.*` (12) | `get_mtr_next_trains`, `get_kmb_eta_by_stop`, `get_kmb_eta_by_route_stop`, `get_citybus_eta_by_route_stop`, `get_citybus_route_stops`, `get_gmb_eta`, `find_stops_near_point`, `find_stops_by_name`, `plan_simple_route` (MTR-only), `plan_walking_route`, `plan_journey` (default), `plan_multimodal_journey` (OTP2). v0.5.1 scope tags surface MTR-only / Citybus-only / KMB-only / etc. to the LLM. **No taxi mode** — transit + walking via real HK APIs only. |
| `context.*` (4) | `get_current_weather`, `get_active_warnings`, `get_9day_forecast`, `get_aqhi` |
| `facility.*` (2) | `find_nearby_courts`, `find_nearby_pools` (both live CSDI) |
| `housing.*` (2) | `get_estate_info`, `list_estates_in_district` (live HKHA API) |
| `geo.*` (32) | `address_lookup` (ALS) + 30 per-category POI tools (`find_dentist`, `find_bench`, `find_convenience_store`, …) auto-generated from `_CATEGORIES` (v0.5.0). |
| `csdi.*` (1) | `query_features` (generic ArcGIS FeatureServer query) |
| `meta.*` (3) | `ask_user` (`[FALLBACK]` since v0.5.1), `what_languages_are_supported`, `forget_me` |

**Pipeline capabilities**

- Cantonese particle-heuristic language detection + unicode-script majority + code-switch detection.
- OpenCC `s2hk` / `hk2s` script normalisation.
- Deterministic fast-path classifier for weather / AQI / warnings / chitchat → skips first LLM hop.
- Parallel tool dispatch via `asyncio.gather`, per-session KV-cache pin (`user=session_id` forwarded to LM Studio), streaming final pass token-by-token over WebSocket.
- **Three-stage lifecycle guard rails (v0.5.1)** — declarative engines wrap the LLM-turn at each stage:
  - Pre-execution gate (`smcity/tool_call_gates.py`) rejects ill-shaped tool-call proposals (e.g., leading with `meta.ask_user`) and re-prompts the LLM once with named alternatives.
  - Post-execution chain rule (`smcity/chain_rules.py`) auto-completes known tool chains deterministically (POI category inference across EN / yue / zh-Hant / zh-Hans, no LLM re-roll).
  - Post-synthesis invariant (`smcity/synthesis_invariants.py`) rejects replies that deny non-empty tool data; multilingual denial detector covers all 13 supported languages.
- **Tool scope tags (v0.5.1)** — `ToolScope.{DEFAULT,SPECIALIZED,FALLBACK}` + free-form `domain` auto-render `[…]` markers into every tool description. Disambiguation moves from per-tool prose to structured schema.
- Cantonese few-shot exemplars + 60-entry formal→colloquial deterministic polish → natural HK Cantonese output.
- Harmony-format leak extractor (canonical + bare `tool_name json{…}` forms) with SSRF guard via `known_tool_names`.
- Deterministic source-footer rewriter (strips LLM-invented `src:` lines, appends the real citations).
- SQLite WAL session store with `chmod 600` + PII redaction at ingress (HK phone + HKID); `meta.forget_me` wipes the row.
- Per-session token-bucket rate limiter (30/min refill, burst 10 by default).
- WebSocket origin allow-list + `session_id` regex guard.
- Tool-result TTL cache + structured `tool_call` audit log.

## Security posture (v0.3.1+ hardening)

See [docs/DEPLOY.md](docs/DEPLOY.md) for the full threat model. Short version: Tailscale-only by default (no public Funnel), PII redacted at ingress, chmod-600 session DB, per-session rate limit, WS origin allow-list, SSRF guards on the CSDI query tool. CI runs `pip-audit --strict` + `osv-scanner` on every push.

## Documentation

- [docs/GOAL.md](docs/GOAL.md) — vision + non-negotiables + success criteria
- [docs/PLAN.md](docs/PLAN.md) — phased roadmap
- [docs/PROTOCOL.md](docs/PROTOCOL.md) — WebSocket event schema for robot integration
- [docs/DEPLOY.md](docs/DEPLOY.md) — Tailscale deployment + threat model + LM Studio tuning
- [docs/DATASETS.md](docs/DATASETS.md) — 35-workbook-dataset → tool coverage map
- [docs/ACCURACY_REVIEW.md](docs/ACCURACY_REVIEW.md) — response-quality risk register for the fuzzer
- [docs/architecture/TOOL_CATALOG.md](docs/architecture/TOOL_CATALOG.md) — formal 55-tool registry
- [docs/architecture/ARCHITECTURE.md](docs/architecture/ARCHITECTURE.md) — system architecture, including the v0.5.x lifecycle guard rails (§3.7) and tool scope tags (§3.8)
- [docs/audit/](docs/audit/) — supply-chain, code, enhancement audits (v0.3.0)
- [docs/research/](docs/research/) — data.gov.hk APIs, agentic tool-calling, CSDI endpoints, multilingual stack, prior art
- [CHANGELOG.md](CHANGELOG.md) — per-release notes

## What's still deferred

- **OTP2 graph activation** — code + docker-compose shipped; requires Docker install + GTFS + OSM downloads + ~30 min graph build on your side.
- **NLLB-200 translation fallback** for non-CJK languages (currently the LLM itself does MT).
- **GlotLID-3 transformer** for precise fr/de/tl/id/vi detection (currently default to `eng`).
- **S512 iB1000 topographic map via CSDI** — not yet needed (OSM + ALS cover place lookup).
