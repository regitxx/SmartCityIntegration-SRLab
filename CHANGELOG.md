# Changelog

All notable changes to this project are documented here. Versions follow [SemVer](https://semver.org/).

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
