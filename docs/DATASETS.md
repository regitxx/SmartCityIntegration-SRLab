# Dataset coverage

Mapping from the lab's `3 - Selected Smart City Data Maps.xlsx` (35 datasets) to the tools that serve them.

**Legend**: ✅ live API · 🟨 bundled (static) · ⏳ deferred to next version

> The Selected Smart City Data Maps workbook is the source of truth for what v0.3+ must support. Most POI categories (S514-S549) come from OpenStreetMap via the Overpass API — that's a single generic tool. Transportation comes from data.gov.hk. Topographic is from CSDI.

---

## Transportation (4 datasets — data.gov.hk)

| ID | Dataset | Status | Tool(s) |
|---|---|---|---|
| S500 | Headway Information of Public Transport Services (GTFS) | ⏳ deferred | needs GTFS parser + static bundle refresh; today we serve live ETAs via KMB/Citybus/MTR/GMB |
| S505 | MTR Routes, Fares and Barrier-free Facilities | ⏳ deferred | not yet; routes + live Next Train covered by `transport.plan_simple_route` and `transport.get_mtr_next_trains`; fares + barrier-free TBD |
| S506 | Fare Table and Timetable of Licensed Ferry Services | ⏳ deferred | needs `transport.get_ferry_schedule` wrapper around TD CSV |
| S507 | Routes and Fares of Public Transport (GeoJSON) | ⏳ deferred | live ETAs already covered per-operator; fares TBD |

**Already shipped beyond the xlsx** (from the initial user brief):

| data.gov.hk source | Status | Tool(s) |
|---|---|---|
| MTR Next Train API (`rt.data.gov.hk/v1/transport/mtr`) | ✅ | `transport.get_mtr_next_trains` |
| KMB ETA API (`data.etabus.gov.hk`) | ✅ | `transport.get_kmb_eta_by_stop`, `transport.get_kmb_eta_by_route_stop` |
| Citybus ETA API (`rt.data.gov.hk/v2/transport/citybus`) | ✅ | `transport.get_citybus_eta_by_route_stop`, `transport.get_citybus_route_stops` |
| GMB ETA API (`data.etagmb.gov.hk`) | ✅ **new in v0.3.0** | `transport.get_gmb_eta` |
| Address Lookup Service (`www.als.gov.hk`) | ✅ | `geo.address_lookup` |
| HKO Weather API (`data.weather.gov.hk`) | ✅ | `context.get_current_weather`, `context.get_active_warnings` |
| HKO 9-day Forecast | ✅ **new in v0.3.0** | `context.get_9day_forecast` |
| EPD AQHI | ✅ | `context.get_aqhi` |

---

## Geography (1 dataset — CSDI Portal)

| ID | Dataset | Status | Tool(s) |
|---|---|---|---|
| S512 | Digital Topographic Map iB1000 | ⏳ deferred | CSDI ArcGIS REST service-ID discovery required; for now we use OSM + ALS for place-lookup needs |

---

## Points of Interest (27 datasets — OSM via Overpass Turbo)

All 27 categories are covered by a single tool, `geo.search_osm_pois`, with a category enum that maps to the Overpass tag filters from the workbook.

| ID | Category | Status | `category` argument |
|---|---|---|---|
| S514 | Convenience Stores | ✅ | `convenience_store` |
| S515 | Supermarkets | ✅ | `supermarket` |
| S516 | Public Toilets | ✅ | `public_toilet` |
| S517 | Places of Worship | ✅ | `place_of_worship` |
| S518 | MTR Station Entrances | ✅ | `mtr_station_entrance` |
| S519 | Recycling Locations | ✅ | `recycling_location` |
| S520 | Veterinarians | ✅ | `veterinarian` |
| S521 | Hardware Stores | ✅ | `hardware_store` |
| S522 | Public Elevators | ✅ | `public_elevator` |
| S523 | Hairdressers | ✅ | `hairdresser` |
| S524 | Clothes Shops | ✅ | `clothes_shop` |
| S525 | Electronics Shops | ✅ | `electronics_shop` |
| S526 | Department Stores | ✅ | `department_store` |
| S527 | Variety Stores | ✅ | `variety_store` |
| S528 | Houseware Shops | ✅ | `houseware_shop` |
| S529 | Beauty Shops | ✅ | `beauty_shop` |
| S530 | Opticians | ✅ | `optician` |
| S531 | Shoe Shops | ✅ | `shoe_shop` |
| S532 | Greengrocers | ✅ | `greengrocer` |
| S533 | Marketplaces | ✅ | `marketplace` |
| S534 | Bookstores | ✅ | `bookstore` |
| S535 | Drinking Water | ✅ | `drinking_water` |
| S536 | Laundries | ✅ | `laundry` |
| S537 | Government Offices | ✅ | `government_office` |
| S538 | Kiosks | ✅ | `kiosk` |
| S541 | Dentists | ✅ | `dentist` |
| S542 | Bookmakers | ✅ | `bookmaker` |

---

## Road Facilities (3 datasets — OSM via Overpass Turbo)

Same unified tool — different category values.

| ID | Category | Status | `category` argument |
|---|---|---|---|
| S543 | Locations with Bench | ✅ | `bench` |
| S544 | Shelters | ✅ | `shelter` |
| S549 | Provision of Handrails | ✅ | `handrail` |

---

## Extras shipped alongside the xlsx list (from the user's original brief)

These aren't in the workbook but answer the original "smart city for a social robot" requirements. They stay bundled because the live equivalents (CSDI ArcGIS services) need service-ID discovery we haven't automated yet:

| Domain | Status | Tool(s) |
|---|---|---|
| LCSD basketball courts | 🟨 bundled (15 courts) | `facility.find_nearby_courts` |
| LCSD swimming pools | 🟨 bundled (10 pools) | `facility.find_nearby_pools` |
| HKHA public housing estates | 🟨 bundled (10 estates) | `housing.get_estate_info`, `housing.list_estates_in_district` |
| MTR line topology (10 lines) | 🟨 bundled | used by `transport.plan_simple_route` |
| MTR station catalog (105 stations, trilingual) | 🟨 bundled | used by the MTR ETA + planner tools |

Upgrade path: once the CSDI ArcGIS service-ID catalog is mapped (service → dataset title lookup), swap each of these to live FeatureServer queries. Tracked as v0.4+ work.

---

## Coverage summary (v0.3.0)

- **30 of 35 workbook datasets** served live: all 27 POI categories + all 3 road facility categories.
- **4 data.gov.hk transport datasets (S500, S505-S507)** deferred to v0.4+.
- **1 CSDI topographic map (S512)** deferred to v0.4+.
- **5 extra live data.gov.hk endpoints** (MTR / KMB / Citybus / GMB / HKO / EPD / ALS) from outside the workbook, covering the original lab brief.
- **3 bundled-data tools** (LCSD × 2 + HKHA × 1) outside the workbook scope; live upgrade tracked for v0.4+.

Total tools in the registry: **25** (v0.2.0 was 22; v0.3.0 adds `geo.search_osm_pois`, `transport.get_gmb_eta`, `context.get_9day_forecast`).
