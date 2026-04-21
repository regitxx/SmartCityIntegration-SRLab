# Tool Catalog — HK Smart City Agent

**Version:** 2026-04-21 · v0.1
**Ownership:** each tool has a single owner handler module.
**Naming:** `{domain}.{verb_object}` · `snake_case` · stable public name.

Domains:
- `transport.*` — public transit, driving, multimodal routing.
- `context.*` — weather, AQI, warnings, traffic overlays.
- `facility.*` — LCSD venues (courts, pools, parks, libraries, etc.).
- `housing.*` — HKHA / RVD datasets.
- `geo.*` — address lookup, coord transforms, POI search.
- `meta.*` — `ask_user`, `what_languages_are_supported`, `get_session_state`.

Every tool MUST declare:
```python
class ToolSpec(BaseModel):
    name: str
    description_en: str
    args_schema: type[BaseModel]
    result_schema: type[BaseModel]
    upstream_langs: set[Literal["en","tc","sc"]]
    ttl_seconds: int
    budget_ms: int      # soft timeout
    cacheable: bool
    safety_class: Literal["read","read_personal","write"]   # v0: all "read"
```

---

## Transport (`transport.*`)

| Tool | Purpose | Upstream | TTL | Budget |
|---|---|---|---|---|
| `transport.get_mtr_next_trains` | Next 4 trains at a MTR heavy-rail station | MTR Next Train | 10 s | 800 ms |
| `transport.get_lrt_next_trains` | Next trains at a Light Rail stop | MTR LRT | 10 s | 800 ms |
| `transport.get_mtr_service_status` | Line disruptions / alerts | MTR service-status page (scrape + cache) | 60 s | 1000 ms |
| `transport.get_kmb_eta_by_stop` | ETAs of all routes at a KMB stop | KMB | 30 s | 800 ms |
| `transport.get_kmb_eta_by_route_stop` | ETA for a specific route at a KMB stop | KMB | 30 s | 800 ms |
| `transport.get_citybus_eta_by_stop` | ETAs at a Citybus stop | Citybus | 30 s | 800 ms |
| `transport.get_citybus_eta_by_route_stop` | Route-specific ETA | Citybus | 30 s | 800 ms |
| `transport.get_nlb_eta` | Lantau bus ETA | NLB | 30 s | 800 ms |
| `transport.get_gmb_eta` | Green minibus ETA | GMB | 30 s | 800 ms |
| `transport.get_ferry_schedule` | Ferry schedule + any live ETA | TD Ferry dataset | 5 m | 1200 ms |
| `transport.get_tram_info` | Best-effort tram schedule (no live feed) | scheduled headway | 60 m | 500 ms |
| `transport.find_stops_near_point` | k-NN over all operator stops | local index | 1 h | 200 ms |
| `transport.find_stops_by_name` | Fuzzy stop-name resolver (EN/繁體/简体/Jyutping) | local index | 1 h | 250 ms |
| `transport.alias_stop` | Cluster cross-operator stops to one logical stop | local index | 1 h | 100 ms |
| `transport.resolve_route_pattern` | route+direction+service_type → stop list | operator | 1 h | 800 ms |
| `transport.get_route_fare` | Fare for OD pair | GTFS fares | 1 h | 500 ms |
| `transport.plan_multimodal_route` | Full journey: walk + transit + walk | OTP2 + overlays | 30 s | 2000 ms |
| `transport.plan_drive_route` | Driving ETA + alt routes | TDAS | 60 s | 1500 ms |
| `transport.plan_walking_route` | Pedestrian path only | Valhalla/OTP | 30 m | 800 ms |
| `transport.plan_barrier_free_route` | Wheelchair-friendly routing | OTP2 + curated accessibility | 30 s | 2500 ms |
| `transport.reachability_isochrone` | "Where can I get in N minutes?" | R5 / OTP isochrones | 5 m | 2500 ms |
| `transport.detect_transfer_time` | Platform-to-platform interchange time at a station | curated dataset | 24 h | 150 ms |
| `transport.last_trip_of_day` | Late-night planner | GTFS + real-time fallback | 60 s | 1500 ms |

## Context (`context.*`)

| Tool | Purpose | Upstream | TTL | Budget |
|---|---|---|---|---|
| `context.get_current_weather` | Temp / humidity / rain / wind now | HKO `rhrread` | 5 m | 800 ms |
| `context.get_9day_forecast` | Multi-day outlook | HKO `fnd` | 1 h | 800 ms |
| `context.get_active_warnings` | Typhoon / rainstorm / thunderstorm / landslip / hot / cold / fire | HKO `warnsum` + `warningInfo` | 60 s | 800 ms |
| `context.get_aqhi` | AQHI at nearest station + forecast band | EPD AQHI | 5 m | 800 ms |
| `context.get_drive_traffic_speed` | Road-segment speed bands | TD JTIS | 2 m | 800 ms |
| `context.get_traffic_snapshot_url` | CCTV JPEG for a road | TD CCTV | 60 s | 200 ms |
| `context.get_live_service_summary` | One-call "anything weird right now?" | fuses MTR + bus alerts + HKO + EPD | 60 s | 1500 ms |

## Facility (`facility.*`)

| Tool | Purpose | Upstream | TTL | Budget |
|---|---|---|---|---|
| `facility.find_nearby` | k-NN over LCSD static catalog (court / pool / pitch / park / library / community hall / museum) | LCSD CSDI | 24 h | 500 ms |
| `facility.get_details` | Full metadata for a facility ID | LCSD CSDI | 24 h | 400 ms |
| `facility.get_availability` | Real-time booking slots | LCSD SmartPLAY | 60 s | 800 ms |
| `facility.get_opening_hours` | Opening hours + holiday schedule | LCSD venue directory | 24 h | 400 ms |

## Housing (`housing.*`)

| Tool | Purpose | Upstream | TTL | Budget |
|---|---|---|---|---|
| `housing.get_estate_info` | Public housing estate profile | HKHA PRH Estates | 24 h | 800 ms |
| `housing.list_estates_in_district` | Estates in a district | HKHA | 24 h | 800 ms |
| `housing.get_property_market_stats` | Rent / price medians by district | RVD | 24 h | 800 ms |
| `housing.get_waitlist_aggregate_stats` | Quarterly average wait time by family type (aggregate only) | HKHA public stats | 24 h | 800 ms |

> **Deliberate gap:** no `housing.check_my_application_status` tool. The agent must redirect to the official eligibility checker + application portal when asked. See `02_*.md` §1.3.

## Geo (`geo.*`)

| Tool | Purpose | Upstream | TTL | Budget |
|---|---|---|---|---|
| `geo.address_lookup` | Free-text address → GeoJSON feature list | ALS | 24 h | 1000 ms |
| `geo.reverse_geocode` | lat/lng → nearest address | CSDI + ALS | 24 h | 800 ms |
| `geo.transform_coords` | HK1980 ↔ WGS84 | Lands Dept Transform | 30 d | 150 ms |
| `geo.location_search` | Place / building / POI search | CSDI Location Search | 24 h | 1000 ms |
| `geo.jyutping_to_text` | Romanized Cantonese → Traditional | pycantonese (local) | 30 d | 100 ms |

## Meta (`meta.*`)

| Tool | Purpose |
|---|---|
| `meta.ask_user` | Emit a clarifying question; writes slot expectation. The only "tool" that doesn't touch upstream data — it's the disambiguation gate. |
| `meta.what_languages_are_supported` | For a given tool name or upstream dataset, report the language-coverage matrix. Used when the user asks "can you answer in Korean?". |
| `meta.get_session_state` | Introspect current slot state (dev / debug). |
| `meta.forget_me` | Wipe the session record + Langfuse trace. |

---

## Invariants

1. All tool names are stable across versions — renames require deprecation plus alias for at least 60 days.
2. All tool args are pydantic-validated at the dispatcher. The LLM never sees raw untyped JSON reach a handler.
3. All tool responses include `source` and `fetched_at` so the response formatter can attach citations.
4. No tool ever returns free-form HTML from an upstream — always extract structured fields.
5. A tool either returns cleanly or raises one of `ToolTimeout`, `ToolRateLimited`, `ToolUpstreamError`, `ToolValidationError`. The dispatcher maps these to user-facing degraded responses.

---

## v0 scope cut (Phase 1 MVP — see `docs/PLAN.md`)

Only these tools ship in v0:

- `transport.get_mtr_next_trains`
- `transport.get_kmb_eta_by_stop`
- `transport.get_citybus_eta_by_stop`
- `transport.find_stops_near_point`
- `transport.find_stops_by_name`
- `transport.plan_multimodal_route` (MTR + bus + walk only)
- `context.get_current_weather`
- `context.get_active_warnings`
- `context.get_aqhi`
- `facility.find_nearby` (basketball courts only at first, then broaden)
- `geo.address_lookup`
- `meta.ask_user`
- `meta.what_languages_are_supported`

That's 13 tools — enough to answer the three hero scenarios in the user's brief:
Sheung Wan → Sha Tin · "basketball court, how to get there?" · "is it OK to go outside right now?".
