# Hong Kong Smart-City Agentic Chat — Transport API Catalog

> **Scope:** Public transport operator APIs surfaced via data.gov.hk (MTR, KMB, Citybus, NLB, GMB, Ferry, Tram, TD) plus the multimodal routing strategy for the agent. Housing/weather/AQI/traffic-overlay/geocoding APIs are in `02_datagovhk_housing_context_apis.md`.
>
> **Verification caveat:** Endpoint paths and rate-limit figures below are the research agent's reconstruction from publicly documented API specs (cited inline). Before production code depends on any path, confirm against the current official spec on data.gov.hk + the operator's developer page. "Open Government License" is the default license unless noted.
>
> **Version:** 2026-04-21 · v1.0

---

## 0. TL;DR — what you need to hit 80% of queries

| Priority | Dataset | Why it matters |
|---|---|---|
| 1 | **MTR Next Train API** | Rail backbone; majority of cross-district journeys |
| 2 | **KMB / LWB ETA API** | Largest bus operator; densest stop network |
| 3 | **Citybus (incl. ex-NWFB) ETA API** | Complementary coverage, HK Island-heavy |
| 4 | **GMB Real-time API** | Green minibuses fill the last-mile gaps |
| 5 | **GTFS static (Transport Department)** | Needed for multimodal routing engine (OTP2/Valhalla) |

The agent should treat MTR + KMB + Citybus as "hot" tools, GMB/NLB/Ferry/Tram as "warm", and everything else as "cold" with just-in-time lookup.

---

## 1. MTR — Mass Transit Railway

### 1.1 Next Train API (heavy rail)
- **Spec (official):** https://opendata.mtr.com.hk/doc/Next_Train_API_Spec_v1.6.pdf
- **Data.gov.hk listing:** https://data.gov.hk/en-data/dataset/mtr-data2-nexttrain-data
- **Endpoint:** `https://rt.data.gov.hk/v1/transport/mtr/getSchedule.php`
  - Query params: `line={AEL|TCL|TML|TKL|EAL|SIL|TWL|ISL|KTL|DRL}`, `sta={STATION_CODE}`, optional `lang={EN|TC}`
- **Format:** JSON
- **Refresh:** real-time (~10 s); response returns up to the next 4 trains in each direction (UP / DOWN).
- **Auth:** open; no API key observed.
- **Language:** EN + 繁體 full; 简体 not supported at the JSON level (destination/station names).

**Answers:** next N trains at a station, destination, ETA in minutes, sequence, platform (when present), service status field.

**Pitfalls**
- `direction` is UP/DOWN, not physical platform numbers at interchange stations.
- Some destinations abbreviate on multi-destination services (e.g. East Rail Line peak patterns).
- When service is disrupted, the `status`/`message` fields replace the schedule block — always branch on `status == "1"` (normal) vs `"0"` (alert).

**Example**
```
GET https://rt.data.gov.hk/v1/transport/mtr/getSchedule.php?line=TCL&sta=HOK&lang=EN
```

### 1.2 Light Rail Next Train & ETA
- Endpoint: `https://rt.data.gov.hk/v1/transport/mtr/lrt/getSchedule?station_id={CODE}`
- Separate code set from heavy rail.

### 1.3 MTR Service Status (no stable JSON API)
- Primary: https://www.mtr.com.hk/en/customer/main/service_status.html
- Twitter @mtrupdate as de facto push feed.
- **Agent strategy:** poll the status page every 60 s with a TTL cache; surface disruptions alongside Next Train results; never promise routing certainty when a line is flagged.

---

## 2. KMB + LWB — Kowloon Motor Bus & Long Win Bus

- **Spec:** https://data.etabus.gov.hk/datagovhk/kmb_eta_api_specification.pdf
- **Data.gov.hk:** https://data.gov.hk/en-data/dataset/hk-kmb-kmb-kmb-eta-and-route-stops
- **Base:** `https://data.etabus.gov.hk/v1/transport/kmb/`
  - `route-list/` — all routes (static-ish)
  - `route/{route}/{direction}/{service_type}` — route metadata
  - `route-stop/{route}/{direction}/{service_type}` — stop sequence
  - `stop/{stop_id}` — stop metadata
  - `stop-eta/{stop_id}` — ETAs for all routes calling at a stop
  - `eta/{stop_id}/{route}/{service_type}` — ETAs for a specific route at a stop
- **Format:** JSON · **Auth:** open · **Refresh:** ~60 s ETA
- **Language:** EN + 繁體 + 简体 all returned in the same payload (`name_en` / `name_tc` / `name_sc`).
- **Stop IDs:** 5-character opaque strings — must be resolved via `route-stop/` or geocoding against lat/lng in `stop/` feeds.

**Answers:** full route catalog, route patterns, per-stop ETAs, stop geolocation.

**Pitfalls**
- KMB and LWB share this API (LWB routes appear with operator code `LWB` in `route-list`).
- `service_type` distinguishes special trips (school, peak, night); the agent must present the right variant or results will lie.
- ETAs can be `null` when no trip is predicted within the horizon — don't invent one.

---

## 3. Citybus (incl. legacy NWFB routes)

- **Spec:** https://www.citybus.com.hk/datagovhk/bus_eta_api_specifications.pdf
- **Data.gov.hk:** https://data.gov.hk/en-data/dataset/hk-citybus-routeseta-citybus-route-eta
- **Base:** `https://rt.data.gov.hk/v2/transport/citybus/`
  - `route/CTB` — route list (all Citybus routes, includes former NWFB after the 2023 merger)
  - `route-stop/CTB/{route}/{direction}` — stop list
  - `stop/{stop_id}` — stop meta
  - `eta/CTB/{stop_id}/{route}` — ETAs
- **Format:** JSON · **Auth:** open · **Refresh:** 30–60 s
- **Language:** EN + 繁體 + 简体 all in response.
- **Stop IDs:** typically 6 digits; **not interchangeable with KMB stop IDs**.

---

## 4. NLB — New Lantao Bus (Lantau & outlying islands)
- **Base:** `https://rt.data.gov.hk/v2/transport/nlb/` (route-list/route/stop/eta variants)
- JSON, open, EN + 繁體 + 简体.

## 5. GMB — Green Minibuses (Transport Department)
- **Data.gov.hk:** https://data.gov.hk/en-data/dataset/hk-td-tis_21-etagmb
- **Base:** `https://data.etagmb.gov.hk/`
  - `/route/` route catalog, `/route/{region}/{route_code}` route detail, `/stop-route/{stop_id}` stops → routes, `/eta/route-stop/{route_id}/{stop_id}` ETAs
- JSON, open, 60 s refresh, EN + 繁體 + 简体.
- Essential for last-mile coverage in New Territories / Kowloon fringe.
- **Red minibuses are NOT covered** — no real-time feed exists; the agent should surface this gap honestly and, if asked, point to community projects (e.g. `hkbus/hk-bus-crawling` on GitHub) but not promise real-time ETAs.

## 6. Ferries
- **Data.gov.hk:** https://data.gov.hk/en-data/dataset/hk-td-tis_24-ferry-service-timetables
- Operators: Star Ferry, Sun Ferry, HKKF, Fortune Ferry, Coral Sea Ferry (outlying islands).
- Most publish static schedules (CSV/JSON); a handful expose real-time. Treat as "scheduled" by default in the agent.

## 7. Hong Kong Tramways
- **No official API on data.gov.hk** as of 2026-04.
- Options: (a) community Node.js scrapers, (b) Citymapper partnership data, (c) ignore real-time and use scheduled headway (~4–8 min) + route geometry from OSM.
- Agent should mark tram answers "best-effort, no live feed".

## 8. Transport Department — unified transport data
- **Journey Planner (HKeMobility):** https://www.hkemobility.gov.hk/ — app-centric; some underlying TDAS endpoints are documented in §5 of `02_datagovhk_housing_context_apis.md`.
- **GTFS static bundle:** TD publishes periodic GTFS for bus/minibus/MTR/ferry covering routes, stops, schedules, fares. This is the backbone feed for an offline multimodal router.
- **GTFS-RT:** per-operator ETA JSON feeds above are the real-time substitute. There is no single unified GTFS-RT for HK yet — the router must fuse feeds per mode.

---

## 9. Multimodal Routing — architecture options

HK has no unified public multimodal planner exposed via open API. The agent needs one.

| Engine | Pros | Cons | Fit for HK |
|---|---|---|---|
| **OpenTripPlanner 2 (OTP2)** | Mature GTFS + GTFS-RT support; widely used by transit agencies; isochrones; street + transit fused on one graph. | Heavier JVM footprint (~4–8 GB RAM). | **Recommended primary.** Proven with HK GTFS by community deployments. |
| **Valhalla** | Low-memory, tile-based; strong walking/cycling; flexible costing. | Transit support is less rich than OTP. | Good walking/driving companion alongside OTP2. |
| **GraphHopper** | Very fast driving + walking; clean Java API. | Commercial transit module is paid; OSS transit support limited. | Useful as driving + walking fallback. |
| **R5 (Conveyal)** | Best-in-class accessibility isochrones & time-matrix analytics. | Not designed for real-time single-trip queries. | Secondary tool for "how many courts reachable in 20 min?". |

**Recommended composition (hot path):**
1. Walking legs → Valhalla (or OTP's internal walk router).
2. Transit legs → OTP2 over combined HK GTFS + our own operator ETA feeds overlaid for real-time adjustments.
3. Driving legs → TDAS for HK-authoritative drive times (§5 in `02_*.md`).
4. Fallback schedule-only when a real-time feed is missing.

**Real-time overlay strategy:**
- Poll MTR / KMB / Citybus / GMB / NLB ETA APIs every 10–30 s per relevant stop-route pair (only while a query is active — no background floods).
- Merge predicted delay into OTP2's "transit leg" via its real-time updater (GTFS-RT trip updates, injected by a small adapter that converts operator JSON → GTFS-RT).
- If a feed is down, flag the leg as "scheduled-only"; do not hide the uncertainty from the user.

---

## 10. Recommended LLM tool catalog (~25 tools)

Transport-specific (append to the context tools in `02_*.md`):

| Tool | Purpose | Upstream |
|---|---|---|
| `get_mtr_next_trains` | Next trains at a MTR / LR station | MTR Next Train |
| `get_mtr_service_status` | Active line disruptions | scrape + cache |
| `get_kmb_eta_by_stop` | ETAs for all routes at a KMB stop | KMB |
| `get_kmb_eta_by_route_stop` | ETAs for a specific route+stop | KMB |
| `get_citybus_eta_by_stop` | ETAs at a Citybus stop | Citybus |
| `get_citybus_eta_by_route_stop` | Route-specific ETA | Citybus |
| `get_nlb_eta` | Lantau bus ETA | NLB |
| `get_gmb_eta` | Green minibus ETA | GMB |
| `get_ferry_schedule` | Ferry schedule (+ real-time where available) | TD Ferry dataset |
| `get_tram_schedule` | Tram headway + route (best-effort) | community/OSM |
| `find_stops_near_point` | k-NN over stops for a lat/lng | local index of GTFS + operator feeds |
| `find_stops_by_name` | Fuzzy stop-name resolver (EN/繁體/简体/Jyutping) | local index |
| `resolve_route_pattern` | route+direction+service_type → stop list | operator |
| `get_route_fare` | Fare for origin/destination pair | GTFS fares or operator |
| `plan_multimodal_route` | Full trip (walk+transit+walk) | OTP2 + overlays |
| `plan_drive_route` | Driving ETA + tunnel option | TDAS |
| `plan_walking_route` | Pedestrian path only | Valhalla/OTP |
| `plan_barrier_free_route` | Wheelchair-friendly routing | OTP2 accessibility + LCSD metadata |
| `reachability_isochrone` | "Where can I go in N minutes from X?" | R5 / OTP isochrones |
| `validate_stop_id` | Cross-operator stop-ID sanity check | local index |
| `list_nearby_pois` | Attractions / facilities near a stop or point | LCSD + OSM |
| `get_live_service_summary` | One-call "anything weird on the network?" | fuses MTR + bus alerts + HKO warnings |
| `get_last_trip_of_day` | Late-night planner | GTFS + real-time fallback |
| `detect_transfer_time` | Platform-to-platform interchange time at a station | curated dataset |
| `alias_stop_across_operators` | Cluster stops within 100 m as the same logical stop | local index |

The `alias_stop_across_operators` tool is critical: operator stop IDs don't overlap. Cluster stops by 100 m radius + name similarity, assign a stable internal `smc:stop:<hash>` ID the LLM uses, and translate back when calling each upstream.

---

## 11. Known gaps + mitigations

| Gap | Mitigation |
|---|---|
| No unified stop ID across operators | Build alias table (§10: `alias_stop_across_operators`) |
| No official Tram real-time | Schedule-only + "best effort" disclosure |
| No red-minibus real-time | Honest gap; suggest GMB/bus alternatives |
| No door-to-door multimodal planner from gov | Deploy OTP2 + Valhalla locally, fuse feeds |
| MTR alerts only via HTML | Scraper + TTL cache; monitor MTR Twitter as secondary |
| Ferry real-time patchy | Schedule-first; mark legs "scheduled" |
| Road-closure reporting informal | See `02_*.md` §5.4 |
| Stop-name romanization inconsistent | Run EN / 繁體 / 简体 / Jyutping into the alias resolver; prefer 繁體 canonicalization for HK |

---

## 12. Implementation roadmap (bus/rail/boat only — full roadmap in `PLAN.md`)

- **Phase 1 (MVP, weeks 1–4):** MTR Next Train + KMB + Citybus + OTP2 + EN / 繁體. Enough to answer the "Sheung Wan → Sha Tin" example end-to-end.
- **Phase 2 (weeks 5–8):** Ferry + GMB + NLB; add walking legs via Valhalla.
- **Phase 3 (weeks 9–12):** Weather / AQI / Traffic overlays into route answers (from `02_*.md`).
- **Phase 4 (weeks 13–16):** Tram best-effort, R5 isochrones for "things near me in X minutes".
- **Phase 5 (weeks 17–20):** 简体 locale first-class; polish; evaluation harness.

---

## 13. Canonical citations

- data.gov.hk API spec: https://data.gov.hk/en/help/api-spec
- MTR Next Train v1.6 spec: https://opendata.mtr.com.hk/doc/Next_Train_API_Spec_v1.6.pdf
- MTR data.gov.hk listing: https://data.gov.hk/en-data/dataset/mtr-data2-nexttrain-data
- KMB spec: https://data.etabus.gov.hk/datagovhk/kmb_eta_api_specification.pdf
- KMB listing: https://data.gov.hk/en-data/dataset/hk-kmb-kmb-kmb-eta-and-route-stops
- Citybus spec: https://www.citybus.com.hk/datagovhk/bus_eta_api_specifications.pdf
- Citybus listing: https://data.gov.hk/en-data/dataset/hk-citybus-routeseta-citybus-route-eta
- GMB listing: https://data.gov.hk/en-data/dataset/hk-td-tis_21-etagmb
- NLB: via data.gov.hk transport index
- TD Ferry timetable: https://data.gov.hk/en-data/dataset/hk-td-tis_24-ferry-service-timetables
- HKeMobility: https://www.hkemobility.gov.hk/
- OpenTripPlanner 2: https://docs.opentripplanner.org/
- Valhalla: https://valhalla.github.io/valhalla/
- R5 / Conveyal: https://github.com/conveyal/r5
