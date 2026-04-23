# Tool Catalog — HK Smart City Agent

**Version:** v0.4.4 · 2026-04-23
**Ownership:** each tool has a single owner handler module under `smcity/tools/`.
**Naming:** `{domain}.{verb_object}` · `snake_case` · stable public name.

Domains:
- `transport.*` — public transit + journey planning (MTR / KMB / Citybus / GMB / walk / taxi + OTP2 multimodal).
- `context.*` — weather, AQHI, warnings, forecasts.
- `facility.*` — LCSD basketball courts + swimming pools (live CSDI).
- `housing.*` — HKHA public housing estates (live Housing Authority API).
- `geo.*` — address lookup + OSM POI search.
- `csdi.*` — generic ArcGIS FeatureServer querier.
- `meta.*` — clarification, language coverage introspection, session forget.

Every tool declares a `ToolSpec` (see `smcity/tools/registry.py`) with:
```python
name                 str          # stable, snake_case.domain
description_en       str          # goes into the OpenAI tool schema
args_schema          pydantic     # validated at dispatch
result_schema        pydantic     # normalised output
handler              async def    # the worker
ttl_seconds          int          # result cache TTL
budget_ms            int          # soft timeout
upstream_langs       frozenset    # native EN/繁體/简体 support
upstream             str          # human-readable source (for citations)
cacheable            bool         # opt out of TTL cache per-call
safety_class         str          # "read" (v0 only writes are meta.forget_me)
```

---

## Transport (`transport.*`) — 12 tools

| Tool | Purpose | Upstream | TTL |
|---|---|---|---|
| `transport.get_mtr_next_trains` | Next trains at an MTR station (fuzzy station resolver over 105 stations, trilingual) | rt.data.gov.hk/v1/transport/mtr | 30 s |
| `transport.get_kmb_eta_by_stop` | ETAs of all routes at a KMB stop (by name or stop_id; lazy-loaded 6715-stop catalog) | data.etabus.gov.hk | 30 s |
| `transport.get_kmb_eta_by_route_stop` | ETA of a specific route at a KMB stop | data.etabus.gov.hk | 30 s |
| `transport.get_citybus_eta_by_route_stop` | Live Citybus ETA for a (route, stop) pair | rt.data.gov.hk/v2/transport/citybus | 30 s |
| `transport.get_citybus_route_stops` | Stops along a Citybus route + direction | rt.data.gov.hk/v2/transport/citybus | 1 h |
| `transport.get_gmb_eta` | Green minibus live ETA (two-hop: route lookup → eta) | data.etagmb.gov.hk | 30 s |
| `transport.find_stops_near_point` | k-NN over KMB + MTR stop coords | local catalogs | 1 h |
| `transport.find_stops_by_name` | Fuzzy name search across KMB + MTR | local catalogs | 1 h |
| `transport.plan_simple_route` | Dijkstra walk → MTR → walk (fast path, MTR-only) | `data/mtr_lines.json` topology | 1 h |
| `transport.plan_walking_route` | Walking-only plan with haversine + pace | local geometry | 1 h |
| `transport.plan_taxi_estimate` | Taxi distance + fare (HK 2026 tariff: $27 flag + $1.90/200 m) | local | 1 h |
| `transport.plan_journey` | Unified "best of walk / MTR / taxi" for free-form queries | local | 1 h |
| `transport.plan_multimodal_journey` | **True multimodal** walk + bus + rail + ferry via OTP2 sidecar | OTP2 HTTP (`otp/README.md`) | 60 s |

**Why two planners?** `plan_simple_route` is the fast path — no external deps, <100 ms, MTR-dominated queries answered without a sidecar. `plan_multimodal_journey` handles bus/ferry/minibus/etc. when OTP2 is running; raises a clean upstream error with a fallback hint when it's not.

## Context (`context.*`) — 4 tools

| Tool | Purpose | Upstream | TTL |
|---|---|---|---|
| `context.get_current_weather` | Temp / humidity / past-hour rainfall / UV at HKO | HKO `rhrread` | 5 m |
| `context.get_active_warnings` | Typhoon / rainstorm / thunderstorm / landslip / heat / cold signals in effect | HKO `warnsum` | 60 s |
| `context.get_9day_forecast` | Multi-day outlook: max/min temp, humidity range, wind, rain prob | HKO `fnd` | 1 h |
| `context.get_aqhi` | Per-station Air Quality Health Index + risk band | EPD aqhi_ind_rss (XML) | 5 m |

## Facility (`facility.*`) — 2 tools (live)

| Tool | Purpose | Upstream | TTL |
|---|---|---|---|
| `facility.find_nearby_courts` | LCSD basketball courts by lat/lng + radius, district, or fuzzy name (305 venues, 19 districts) | CSDI FeatureServer `lcsd_rcd_1629267205215_38105` | 24 h |
| `facility.find_nearby_pools` | LCSD swimming pools — same filters (46 pools) | CSDI FeatureServer `lcsd_rcd_1634540558875_77434` | 24 h |

Backed by a 24 h module-level catalog cache; first call per process fetches, subsequent calls hit RAM.

## Housing (`housing.*`) — 2 tools (live)

| Tool | Purpose | Upstream | TTL |
|---|---|---|---|
| `housing.get_estate_info` | Fuzzy EN/繁體 search over all 241 HKHA estates; TC names via `data/hkha_name_map_tc.json` overlay | data.housingauthority.gov.hk | 24 h |
| `housing.list_estates_in_district` | All estates in a district (with optional region filter) | data.housingauthority.gov.hk | 24 h |

> **Deliberate gap:** no `housing.check_my_application_status` tool — personal eligibility is redirected to the official checker per the system prompt's safety rule.

## Geo (`geo.*`) — 2 tools

| Tool | Purpose | Upstream | TTL |
|---|---|---|---|
| `geo.address_lookup` | Free-text address → GeoJSON feature + lat/lng (EN + 繁體) | www.als.gov.hk | 24 h |
| `geo.search_osm_pois` | Unified Overpass search across **30 workbook categories** (S514-S549): toilets, convenience, worship, MTR entrances, water fountains, benches, shelters, dentists, etc. | overpass-api.de | 1 h |

## CSDI (`csdi.*`) — 1 tool

| Tool | Purpose | Upstream | TTL |
|---|---|---|---|
| `csdi.query_features` | Generic read of any registered CSDI ArcGIS FeatureServer dataset (`lcsd_basketball_courts` / `lcsd_swimming_pools`); SSRF-safe by whitelist | portal.csdi.gov.hk | 24 h |

## Meta (`meta.*`) — 3 tools

| Tool | Purpose |
|---|---|
| `meta.ask_user` | Emit a clarification prompt with a slot hint — the only tool that returns a question rather than data. |
| `meta.what_languages_are_supported` | Introspect the per-tool `upstream_langs` matrix. |
| `meta.forget_me` | Wipe the caller's session row (GDPR-style "right to be forgotten"). |

---

## Invariants

1. **Stable names.** Renames require a deprecation alias for ≥60 days.
2. **Typed args at the boundary.** `ToolRegistry.dispatch` validates `raw_args` against `args_schema` before any handler runs. The LLM never reaches a handler with untyped JSON.
3. **Cited sources.** Every successful tool call produces a `Citation(tool, upstream, fetched_at, upstream_langs, translation_applied)` that the orchestrator attaches to the final reply's `src: …` footer.
4. **No raw HTML leaking to the user.** Handlers parse upstream formats (JSON / XML / OSM Overpass QL) into structured fields. No `<br/>`, no CDATA escapes reach the user.
5. **Typed failures.** Handlers raise exactly one of `ToolTimeoutError`, `ToolRateLimitedError`, `ToolUpstreamError`, `ToolValidationError` — the dispatcher catches these and converts to a `ToolResult(status=…)` with `status != "ok"`.

---

## Adversarial testing

`smcity_fuzz/` (v0.4.0+) drives this registry with LLM-generated questions across 5 personas × 22 topics × 4 languages, grades the replies with an LLM-as-judge, and exports Markdown handoff reports. The judge is explicitly prompted to describe defects only — never to propose code fixes. See `smcity_fuzz/judge.py` + `smcity_fuzz/export.py`.

## Upstream coverage ledger

See `smcity/langrouter/coverage.py` (`DATASET_COVERAGE`) for the native-language matrix per tool. When a user query arrives in a language an upstream doesn't natively serve, `choose_query_lang()` picks the closest supported tongue and sets `translation_applied=True` on the resulting citation.
