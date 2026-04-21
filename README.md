# SmartCityIntegration — HK Lab of Social Robotics

Agentic chat system that answers Hong Kong smart-city questions — transport (MTR / KMB / Citybus / multimodal walk+MTR), public facilities (LCSD basketball courts + pools), public housing (HKHA estates), weather / air quality / warnings — by calling [data.gov.hk](https://data.gov.hk/en/) APIs through a strict tool registry.

Cantonese-first (廣東話), 100% language coverage from v0 via a fallback-translation layer, fast (p50 ~ 3.8 s on fast-path intents, ~7 s on full path), clean seams for future platform integration.

## Status — **v0.1.0** · shipped 2026-04-21

18 live tools, 9 phases on `main`, 129 unit tests + 7 live integration tests green, ruff + mypy strict clean across 44 source files.

LLM backbone: `openai/gpt-oss-120b` served by LM Studio on the lab's Mac Studio (`earnests-mac-studio.taila366aa.ts.net:1234`) over Tailscale. OpenAI-compatible; native tool-calling + streaming verified.

## Try it

Prereqs:
- macOS or Linux
- [`uv`](https://github.com/astral-sh/uv) — `brew install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Optional: [`just`](https://github.com/casey/just) — `brew install just`
- Tailscale connected and on the **Earnest Design Lab** tailnet so the Mac Studio endpoint resolves

```bash
cp .env.example .env                      # adjust if needed
uv sync --extra dev                       # or: just install
uv run python -m scripts.llm_ping         # smoke-test LM Studio
uv run pytest -q -m "not integration"     # unit suite
uv run uvicorn smcity.app:app --reload --port 8080
# open http://localhost:8080
```

All checks: `just check` (ruff + mypy + unit tests). Integration suite (live data.gov.hk + LM Studio): `just integration`.

## Demos

**Cantonese MTR routing (hero scenario):**
```
> 我想由上環去沙田，點樣搭 MTR 最快？

上環搭港島綫往金鐘，約 4 分鐘到金鐘。
喺金鐘轉東鐵綫直達沙田，大約再坐 12 分鐘。
全程大約 21 分鐘，係最快嘅路線。
src: transport_plan_simple_route
```

**Cantonese basketball query:**
```
> 沙田附近有冇籃球場？

沙田有兩個免費籃球場，分別係「沙田賽馬會泳池籃球場」
同埋「沙田運動場籃球場」。
src: facility_find_nearby_courts
```

**Fast-path weather (~3.8 s, one LLM hop, no first hop):**
```
> 而家天氣點呀？

依家香港係 27 度，濕度 76%，過去一個鐘冇雨，紫外線指數好低（0.4）。
src: hko.rhrread
```

**Chitchat (3 ms, zero LLM calls):**
```
> hi
Hi — ask me about HK transport, weather, air quality, courts, pools, or housing.
```

## Layout

```
smcity/
├── app.py                    FastAPI — /health, /turn, /ws/:session_id, static UI
├── llm.py                    LM Studio client (chat + chat_stream + ping)
├── orchestrator.py           per-turn pipeline: detect → classify → tools → stream
├── classifier.py             deterministic fast-path (weather/aqi/warnings/chitchat)
├── cantonese_polish.py       formal→colloquial post-pass (50+ subs, 7 regex rules)
├── langrouter/
│   ├── detect.py             particle heuristic + unicode-script majority
│   ├── normalize.py          OpenCC s2hk / hk2s
│   └── coverage.py           per-tool language-support matrix
├── slots.py + session.py     SessionSlots + SQLite WAL + PII redaction
├── prompts.py                system prompt + Cantonese few-shot exemplars
├── schemas.py                public pydantic request/response models
├── settings.py               pydantic-settings config (env-driven)
└── tools/
    ├── registry.py           ToolSpec + async dispatcher + typed errors
    ├── geo.py                geo.address_lookup (ALS)
    ├── transport.py          transport.get_mtr_next_trains
    ├── transport_kmb.py      KMB ETA (stop + route-stop) + stop catalog
    ├── transport_citybus.py  Citybus ETA + route-stops
    ├── transport_search.py   find_stops_near_point + find_stops_by_name
    ├── transport_planner.py  plan_simple_route (Dijkstra over MTR graph)
    ├── context.py            HKO weather + warnings + EPD AQHI
    ├── facility.py           LCSD basketball courts + swimming pools
    ├── housing.py            HKHA estates lookups
    └── meta.py               ask_user + what_languages_are_supported
web/                          archive-underground UI (vanilla JS + WebSocket)
data/                         static reference data (MTR stations + lines,
                              LCSD courts + pools, HKHA estates)
tests/                        129 unit tests + 7 live integration tests + golden set
docs/                         GOAL · PLAN · OPEN_QUESTIONS · PROTOCOL · DEPLOY ·
                              architecture/* · research/*
```

## Feature inventory (v0.1.0)

**Tool registry — 18 tools**

| domain | tools |
|---|---|
| `transport.*` | `get_mtr_next_trains`, `get_kmb_eta_by_stop`, `get_kmb_eta_by_route_stop`, `get_citybus_eta_by_route_stop`, `get_citybus_route_stops`, `find_stops_near_point`, `find_stops_by_name`, `plan_simple_route` |
| `context.*` | `get_current_weather`, `get_active_warnings`, `get_aqhi` |
| `facility.*` | `find_nearby_courts`, `find_nearby_pools` |
| `housing.*` | `get_estate_info`, `list_estates_in_district` |
| `geo.*` | `address_lookup` |
| `meta.*` | `ask_user`, `what_languages_are_supported` |

**Pipeline capabilities**

- Cantonese particle-heuristic language detection (0.92 confidence on `嘅/喺/咗/冇/佢/唔/咁/㗎/喎/囉/喇/咋/啦/啫/嘞/咩/係咪/嗰/點樣/乜嘢/邊度/邊個/幾多/唔該/幾時/依家/而家/點呀/點先/啲嘢`) + unicode-script majority + code-switch detection.
- OpenCC `s2hk` / `hk2s` script normalisation (HK-correct — preserves Cantonese characters that `s2t` would break).
- Deterministic fast-path classifier for weather / AQI / warnings / chitchat → skips first LLM hop, cuts latency 60%+ on those intents.
- Parallel tool dispatch via `asyncio.gather`, per-session KV-cache pin (`user=session_id` forwarded to LM Studio), streaming final LLM pass token-by-token over the WebSocket.
- Cantonese few-shot exemplars + 50-entry formal-to-colloquial deterministic post-pass — natural HK Cantonese output.
- SQLite WAL session store, PII redaction at ingress, `meta.forget_me` path.
- Language-coverage matrix per tool: Cantonese is never natively served by data.gov.hk and is flagged `translation_applied: true` by default — users see the transparency chip on every answer.

## Documentation

- [docs/GOAL.md](docs/GOAL.md) — vision + non-negotiables + success criteria
- [docs/PLAN.md](docs/PLAN.md) — phased roadmap (all of 0 through 2c shipped)
- [docs/PROTOCOL.md](docs/PROTOCOL.md) — WebSocket event schema for robot integration
- [docs/DEPLOY.md](docs/DEPLOY.md) — Tailscale Serve deployment + threat model
- [docs/OPEN_QUESTIONS.md](docs/OPEN_QUESTIONS.md) — outstanding decisions
- [docs/architecture/ARCHITECTURE.md](docs/architecture/ARCHITECTURE.md) — block diagram + component contracts
- [docs/architecture/TOOL_CATALOG.md](docs/architecture/TOOL_CATALOG.md) — formal tool registry
- [docs/architecture/UI_STYLE.md](docs/architecture/UI_STYLE.md) — chat UI visual spec
- [docs/research/](docs/research/) — data.gov.hk APIs, agentic tool-calling, multilingual stack, prior art
- [CHANGELOG.md](CHANGELOG.md) — release notes

## What's deferred (tracked in docs/PLAN.md)

- **OpenTripPlanner 2 sidecar** — true multimodal (bus + minibus + ferry + tram) routing over HK GTFS. The v0.1 simple planner + KMB/Citybus ETA tools + LLM composition cover the vast majority of queries. OTP2 is an upgrade path, not a blocker.
- **NLLB-200 translation fallback** for non-CJK languages (currently the LLM itself does MT).
- **HIT-TMG/LID-HK transformer** for precise fr/de/tl/id/vi detection (currently default to `eng`).
- **Speculative decoding** (`gpt-oss-20b` as draft model) — needs a second model loaded in LM Studio.
