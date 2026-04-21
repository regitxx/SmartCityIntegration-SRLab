# Hong Kong Smart-City Agentic Chat System
## Housing, Facilities, Weather, AQI, Traffic & Geocoding API Catalog

> **Scope:** Housing + contextual signals (weather / AQI / traffic / facilities / geocoding). Transport-operator APIs (MTR/KMB/Citybus/ferry) are covered in `01_datagovhk_transport_apis.md`.
>
> **Caveat:** Some endpoint paths, dataset slugs, and rate-limit figures below are the agent's best reconstruction from public documentation. Before any production code calls an endpoint, re-verify the exact URL on data.gov.hk and against the provider's current API spec. Known-good, officially published URLs are cited near each entry.
>
> **Version:** 2026-04-21 · v1.0

---

## 1. HOUSING & PROPERTY

### 1.1 Public Housing Estates & Locations
**Name:** Location and Profile of Public Housing Estates
**Provider:** Hong Kong Housing Authority (HA)
**Data.gov.hk URL:** https://data.gov.hk/en-data/dataset/hk-housing-eslocator-eslocator
**Direct Provider:** https://www.housingauthority.gov.hk/en/about-us/publications-and-statistics/open-data-plan/index.html

**Endpoints & Format**
- Base API: `https://data.housingauthority.gov.hk/psi/rest/criteriafilter` (verify slug live)
- Resources: `prh-estates`, `hos-courts`, `shopping-centres`, `flatted-factory`
- Formats: JSON / CSV / XML — GET

**Access:** Open · OGL v3.0 · static (updated when new estates/blocks commissioned)

**Language:** EN full · 繁體 partial (estate names bilingual, metadata EN-only) · 简体 no

**Answers:** which estates near me, estate profile, management office, building count.

**Pitfalls:**
- Waiting list and allocation data are NOT public (form-only, see §1.3).
- Flat-unit details require the PRH Stock API (§1.2).
- API returns WGS84; some provider internal datasets use HK1980 Grid.

**Example**
```
GET https://data.housingauthority.gov.hk/psi/rest/criteriafilter?query={"resourceType":"prh-estates"}&responseFormat=json
```
```json
{ "estateId":"E001","estateName":"Choi Hung Estate|彩虹邨","district":"Kwun Tong|觀塘",
  "region":"Kowloon","latitude":22.3045,"longitude":114.2154,"blocks":7,"flats":2800 }
```

---

### 1.2 Public Rental Housing Stock
**Name:** PRH Stock (Housing Authority)
**Data.gov.hk URL:** https://data.gov.hk/en-data/dataset/hk-housing-emms-emms-housing-stock

- Accessed via data.gov.hk API v2: `https://api.data.gov.hk/v2/filter` (dataset id per data.gov.hk page)
- CSV/JSON/XML · Open · OGL v3.0 · monthly or quarterly
- EN + 繁體 bilingual field names; 简体 no
- Answers: number of flats per estate/block, floor area, elevator presence.
- Pitfall: historical snapshots only; flat numbers may drift.

---

### 1.3 PRH Waiting List & Eligibility — **NO API, FORM-ONLY**
- Eligibility checker: https://www.housingauthority.gov.hk/en/flat-application/application-guide/eligibility-checker/index.html
- Application guide: https://www.housingauthority.gov.hk/en/flat-application/application-guide/ordinary-families/index.html
- Allocation status: https://www.housingauthority.gov.hk/en/flat-application/allocation-status/index.html
- **Only aggregate stats (quarterly average waiting time) are published** as CSV on data.gov.hk — the agent can consume those, but must **never pretend to check individual application status**. Redirect the user to the official form.

---

### 1.4 Property Market Statistics & Valuation Data
**Provider:** Rating and Valuation Department (RVD)
**Data.gov.hk:** https://data.gov.hk/en-data/dataset/hk-rvd-tsinfo_rvd-property-market-statistics
- `https://api.data.gov.hk/v2/filter` — CSV/JSON/XML
- Monthly (residential) / quarterly (commercial)
- EN full · 繁體 partial (district names) · 简体 no
- Answers: median rent/price, yield, vacancy by district & type.
- Pitfall: aggregated only — no unit-level data; historical span ~5–10 yr.

---

## 2. FACILITIES & VENUES (LCSD)

LCSD operates under two data channels:
- **Static geospatial catalogs** on the **CSDI Portal** (https://portal.csdi.gov.hk) — GeoJSON, KML, CSV.
- **Real-time booking availability** via **SmartPLAY** (https://www.smartplay.lcsd.gov.hk/website/en/) — CSV/JSON feeds published through data.gov.hk.

### 2.1 Basketball Courts

#### 2.1a Court locations (static)
- Data.gov.hk: https://data.gov.hk/en-data/dataset/hk-lcsd-csdi-basketball-courts
- Access: CSDI portal + `https://api.data.gov.hk/v2/filter` (GeoJSON / CSV / JSON)
- EN + 繁體 full · 简体 partial
- Pitfall: CSDI may default to HK1980 Grid — always request/transform to WGS84.

#### 2.1b Real-time session availability (SmartPLAY)
- Data.gov.hk: https://data.gov.hk/en-data/dataset/hk-lcsd-facility-facility-bkbc
- Real-time feed (5–15 min refresh) — CSV/JSON
- **Only covers paid, bookable courts.** Free outdoor courts are in the static directory but have no booking API.
- Pitfall: "Available" = not yet booked, not "empty on arrival".

### 2.2 Sports Centres — https://data.gov.hk/en-data/dataset/hk-lcsd-csdi-sports-centres
Static CSDI catalog (GeoJSON/CSV/JSON). Answers: facility list per centre, hours, contact. Watch seasonal/holiday closures — not always in the feed.

### 2.3 Swimming Pools — https://data.gov.hk/en-data/dataset/hk-lcsd-csdi-swimming-pools
Static. Many outdoor pools seasonal (closed Nov–Mar).

### 2.4 Sports Grounds & Pitches — https://data.gov.hk/en-data/dataset/hk-lcsd-csdi-sports-grounds
Static. Football/netball/tennis pitches, floodlit flag.

### 2.5 Parks, Zoos & Gardens — https://data.gov.hk/en-data/dataset/hk-lcsd-csdi-parks-zoos-gardens
Static. Amenities fields (playground, fitness corner, etc.).

### 2.6 LCSD Venue Directory (Libraries, Community Halls, Museums)
Data.gov.hk: https://data.gov.hk/en-data/dataset/hk-lcsd-venue-venue
Quarterly refresh. Abbreviated type codes (PL=Public Library, CH=Community Hall).

---

## 3. WEATHER & HAZARDS (HKO)

### 3.1 Current Weather & 9-Day Forecast — **primary endpoint**
- Base: `https://data.weather.gov.hk/weatherAPI/opendata/weather.php`
- `?dataType={fnd|rhrread|flw|tc|warnsum}&lang={en|tc|sc}`
- JSON/XML · Open · OGL v3.0
- Current-weather refresh ≈ 10–15 min; 9-day forecast refreshed 5×/day (~03/09/15/21/00 HKT).
- EN + 繁體 full · 简体 supported for many datasets — confirm per `dataType` per request.
- Official PDF: https://www.hko.gov.hk/en/weatherAPI/doc/files/HKO_Open_Data_API_Documentation.pdf

### 3.2 Warnings — Typhoon, Rainstorm, Thunderstorm, Landslip
- `dataType=warnsum` → summary of active warnings (Signal 1/3/8/9/10, Amber/Red/Black rain, thunderstorm, landslip, cold, hot, fire).
- `dataType=warningInfo` → detailed warning text (bilingual).
- Real-time; update within minutes of issue/cancellation.
- Use this as the **"any active city-wide warning?"** tool in the agent.

### 3.3 Seismic / Tsunami — from same HKO Open Data API when relevant. Usually n/a for HK but include a stub tool so "earthquake today?" is answerable.

---

## 4. AIR QUALITY (EPD)

**Name:** AQHI at Individual Air Quality Monitoring Stations
**Data.gov.hk:** https://data.gov.hk/en-data/dataset/hk-epd-airteam-current-aqhi-of-individual-air-quality-monitoring-stations
**Portal:** https://www.aqhi.gov.hk/en/
- Hourly refresh · 18 fixed stations (general + roadside)
- EN full · 繁體 full · 简体 no
- AQHI bands: 1–3 Low · 4–6 Moderate · 7 High · 8–10 Very High · 10+ Serious.
- Expose pollutant breakdown (PM2.5, PM10, O3, NO2, SO2, CO) as secondary fields.
- Pitfall: AQHI is a health index, not raw concentration — if the user wants µg/m³, link to the EPD detailed API or portal.

---

## 5. TRAFFIC & DRIVING

### 5.1 Journey Time & Route (TDAS)
- Provider: Transport Department (HKeMobility)
- Spec: https://tdas-api.hkemobility.gov.hk/tdas/specification/TD_TDAS_API_Specifications.pdf
- Data.gov.hk: https://data.gov.hk/en-data/dataset/hk-td-tis_28-traffic-data-tdas
- Supports `lang` en / tc / sc
- **Driving-only.** Combine with MTR/bus/ferry + walking for multimodal.

### 5.2 Traffic Snapshot Images (CCTV)
- Data.gov.hk: https://data.gov.hk/en-data/dataset/hk-td-tis_2-traffic-snapshot-images
- Base: `https://tdcctv.data.one.gov.hk/{CAMERA_ID}.JPG`
- ~2-minute refresh. Useful as a tool that returns an image URL the UI can render (for visual congestion checks).

### 5.3 Journey Time Indicator System (Real-time road speed)
- Dataset on data.gov.hk exposes segment-level speed bands (green/yellow/red) — useful for quick congestion summaries without image parsing.

### 5.4 Road Closures & Special Traffic Arrangements
- **No formal API.** Ingested via HK gov press releases + HKeMobility app. Treat as "best-effort" and surface uncertainty.

---

## 6. GEOCODING & ADDRESS LOOKUP

### 6.1 Address Lookup Service (ALS)
- Provider: Data Policy Office / Lands Department
- Data.gov.hk: https://data.gov.hk/en-data/dataset/hk-dpo-als_01-als
- Endpoint: `https://www.als.gov.hk/lookup`
- Spec: https://www.als.gov.hk/docs/Data_Specification_for_ALS_GeoJSON_EN.pdf
- Free-text address → GeoJSON FeatureCollection; each feature includes `PremisesAddress.EngPremisesAddress` and `ChiPremisesAddress`.
- **Critical tool** — this is how the agent resolves "Sheung Wan" / "Choi Hung Estate Block A" / "城大創新中心" into coordinates.
- EN + 繁體 full · 简体 no — normalize 简体 → 繁體 via OpenCC before querying.

### 6.2 Coordinate Transformation (HK1980 ↔ WGS84)
- Data.gov.hk: https://data.gov.hk/en-data/dataset/hk-landsd-openmap-coordinates-transformation-api
- Use when mixing legacy geospatial datasets with modern lat/lng tools.

### 6.3 CSDI Location Search API
- https://portal.csdi.gov.hk
- Broader building/place search beyond addresses (e.g. "Peak Tower", "International Commerce Centre").

---

## 7. data.gov.hk GENERAL INFRASTRUCTURE
- Main portal: https://data.gov.hk
- API spec: https://data.gov.hk/en/help/api-spec
- Language path pattern: `https://api.data.gov.hk/{en|tc|sc}/v2/filter?dataset=...`
- Historical archive: `https://app.data.gov.hk/v1/historical-archive/`
- Date format: `YYYYMMDD`, timezone HKT (GMT+8)
- No API key for most datasets. Production code should respect an internal ~10 req/min budget per dataset unless the spec says otherwise.

---

## 8. RECOMMENDED CONTEXT TOOL CATALOG (agent-facing)

| Tool | Purpose | Upstream |
|---|---|---|
| `geocode_address` | Free-text → lat/lng + structured address | ALS |
| `reverse_geocode` | lat/lng → nearest address | CSDI / ALS |
| `transform_coords` | HK1980 ↔ WGS84 | Lands Dept Transform |
| `get_current_weather` | Temp, humidity, rainfall, wind | HKO `rhrread` |
| `get_9day_forecast` | Multi-day outlook | HKO `fnd` |
| `get_active_warnings` | Typhoon / rainstorm / thunderstorm / landslip / hot / cold / fire | HKO `warnsum` + `warningInfo` |
| `get_aqhi` | AQHI by nearest station | EPD AQHI |
| `get_nearby_facilities` | k-NN over LCSD static catalog (court / pool / pitch / park / library) | LCSD CSDI |
| `get_facility_availability` | Real-time booking slots | LCSD SmartPLAY |
| `get_drive_route` | Driving ETA + alt routes | TDAS |
| `get_traffic_snapshot_url` | CCTV JPEG URL | TD snapshots |
| `get_road_speed_bands` | Journey Time Indicator | TD JTIS |
| `get_housing_estate_info` | Public housing profile + location | HA PRH Estates |
| `get_property_market_stats` | Rent/price medians by district | RVD |

---

## 9. BILINGUAL COVERAGE MATRIX

| Dataset | EN | 繁體 | 简体 | Notes |
|---|---|---|---|---|
| HA PRH Estates | Full | Partial | No | names bilingual, metadata EN |
| LCSD static catalogs | Full | Full | Partial | facility names bilingual |
| LCSD SmartPLAY feed | Full | Partial | No | session data EN-heavy |
| ALS Address Lookup | Full | Full | No | normalize 简→繁 before query |
| HKO Weather | Full | Full | Partial | `lang=sc` supported on many endpoints |
| HKO Warnings | Full | Full | Partial | wording fidelity best in 繁體 |
| EPD AQHI | Full | Full | No | station names |
| TDAS | Full | Full | Full | one of the few fully trilingual |
| TD Snapshots | n/a | n/a | n/a | image only |
| RVD Property Stats | Full | Partial | No | mostly EN + Chinese district labels |

---

## 10. GAPS & MITIGATIONS

| Gap | Mitigation |
|---|---|
| No personal waiting list API (§1.3) | Answer with aggregate stats + link to official eligibility checker. Agent must not fabricate individual status. |
| LCSD free-court walk-up availability | Combine static directory + SmartPLAY booking (paid) + disclaimer that outdoor/free courts have no live signal. |
| MTR/bus service alerts lack stable API | See `01_datagovhk_transport_apis.md` for operator-specific alert feeds; fall back to scraping MTR service-status page with TTL cache. |
| Road-closure feed informal | Use press-release RSS + HKeMobility; mark answers "best-effort". |
| 简体 coverage uneven | OpenCC 简→繁 on request; respond in 简体 via LLM when user's locale demands it. |
| HK1980 vs WGS84 confusion | Always normalize to WGS84 internally. |
| 9-day forecast is coarse | For hourly granularity, link to HKO site; agent should say "9-day API is the most granular open source today". |
| TDAS is driving-only | Orchestrate walking + MTR + bus for multimodal (see transport doc). |

---

## 11. IMPLEMENTATION NOTES FOR THE AGENT

- **Caching TTLs:** static facility/housing = 24 h; AQHI / 9-day forecast = 5–10 min; HKO warnings / MTR status = 60 s; TDAS routing = 2 min; CCTV snapshot = 60 s.
- **Coordinate policy:** accept any, normalize to WGS84, persist both if needed for round-trips.
- **Language policy:** detect user language; call APIs with matching `lang` when supported; if not, request `tc` (Traditional) and translate the user-facing summary via LLM.
- **Safety:** never claim to be able to check a person's housing application, medical advisory beyond AQHI band text, or give binding legal/financial advice.
- **Uncertainty:** always include a "source + timestamp" foot on answers so the downstream robot voice can hedge ("according to HKO as of 15:00 …").

---

## 12. KEY CITATIONS

- data.gov.hk (portal & API spec): https://data.gov.hk · https://data.gov.hk/en/help/api-spec
- CSDI Portal: https://portal.csdi.gov.hk
- HKO Open Data API: https://www.hko.gov.hk/en/weatherAPI/doc/files/HKO_Open_Data_API_Documentation.pdf
- EPD AQHI: https://www.aqhi.gov.hk/en/ · https://data.gov.hk/en-data/dataset/hk-epd-airteam-current-aqhi-of-individual-air-quality-monitoring-stations
- Transport Dept TDAS: https://tdas-api.hkemobility.gov.hk/tdas/specification/TD_TDAS_API_Specifications.pdf · https://data.gov.hk/en-data/dataset/hk-td-tis_28-traffic-data-tdas
- TD CCTV: https://data.gov.hk/en-data/dataset/hk-td-tis_2-traffic-snapshot-images
- LCSD CSDI catalogs: see §2 (each dataset page linked)
- LCSD SmartPLAY: https://www.smartplay.lcsd.gov.hk/website/en/
- HKHA Open Data: https://www.housingauthority.gov.hk/en/about-us/publications-and-statistics/open-data-plan/index.html
- ALS: https://www.als.gov.hk · https://www.als.gov.hk/docs/Data_Specification_for_ALS_GeoJSON_EN.pdf
- Lands Dept coord transform: https://data.gov.hk/en-data/dataset/hk-landsd-openmap-coordinates-transformation-api
- RVD stats: https://data.gov.hk/en-data/dataset/hk-rvd-tsinfo_rvd-property-market-statistics
