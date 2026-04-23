# CSDI ArcGIS FeatureServer — endpoint reference (v0.3.3)

**Verified:** 2026-04-23 against portal.csdi.gov.hk with live curl. Every URL and field name below was confirmed to return real data.

## Base URL pattern

```
https://portal.csdi.gov.hk/server/rest/services/common/{DATASET_ID}/FeatureServer/{LAYER}/query
```

Anonymous access. No API key, no rate-limit documentation (treat `exceededTransferLimit` + `Retry-After` as the only signals).

## Query parameters we use

| Param | Value | Purpose |
|---|---|---|
| `where` | SQL WHERE (`1=1` for all) | row filter |
| `outFields` | comma list or `*` | column projection |
| `f` | `json` | response format |
| `returnGeometry` | `true` | include `x`/`y` |
| `outSR` | `4326` | request WGS84 (native is HK1980 EPSG:2326 — server reprojects) |
| `resultRecordCount` | 1–3000 | page size |
| `resultOffset` | int | pagination cursor |
| `geometry`, `geometryType=esriGeometryEnvelope`, `inSR=4326`, `spatialRel=esriSpatialRelIntersects` | envelope filter | bbox query |

Follow pagination until `exceededTransferLimit` is absent or `false`.

## Dataset 1 — LCSD basketball courts

- **ID:** `lcsd_rcd_1629267205215_38105`
- **Layer:** `0`
- **URL:** `https://portal.csdi.gov.hk/server/rest/services/common/lcsd_rcd_1629267205215_38105/FeatureServer/0`
- **maxRecordCount:** 2000
- **Geometry:** point, WGS84 when `outSR=4326`
- **Field naming:** UPPER_SNAKE_CASE (e.g. `NAME_EN`, `NAME_TC`, `ADDRESS_EN`, `ADDRESS_TC`, `DISTRICT`, `No__of_Basketball_Courts_EN` with a double underscore)
- **Sample row (verified):** `NAME_EN="North District Sports Ground", NAME_TC="北區運動場", ADDRESS_EN="26 Tin Ping Road, Sheung Shui", No__of_Basketball_Courts_EN=3.0`

## Dataset 2 — LCSD swimming pools

- **ID:** `lcsd_rcd_1634540558875_77434`
- **Layer:** `0`
- **URL:** `https://portal.csdi.gov.hk/server/rest/services/common/lcsd_rcd_1634540558875_77434/FeatureServer/0`
- **maxRecordCount:** 3000
- **Geometry:** point, WGS84 when `outSR=4326`
- **Field naming:** CamelCase (e.g. `NameEN`, `NameTC`, `AddressEN`, `AddressTC`, `DistrictEN`, `DistrictTC`, `FacilityTypeEN`, `FacilityDetailsEN`, `OpeningHoursEN`, `TelephoneEN`, …)
- **Sample row (verified):** `NameEN="Tuen Mun Swimming Pool", NameTC="屯門游泳池", AddressEN="50 Lung Mun Road, Tuen Mun, N.T."`

**Important:** the two LCSD datasets use **different field-naming conventions** (SNAKE vs Camel). Any cross-dataset code must read the names-per-dataset from `CSDIDataset`, never hard-code them.

## Dataset 3 — HKHA public rental housing estates

**Not on CSDI.** The CSDI portal only lists auxiliary HKHA datasets (open space inside developments, housing construction programme — neither is a current-estate roster).

Canonical live source is the Housing Authority's own JSON API (non-ArcGIS):

- **URL:** `https://data.housingauthority.gov.hk/psi/rest/export/prh-estates?format=json`
- **Shape:** `{"data": [{Estate_Name, District_Name, Region_Name, Type_of_Estate, Year_of_Intake, Type_of_Block, No_of_Blocks, Name_of_Block, No_of_Rental_Flats, Flat_Size_m2, Map_Latitude, Map_Longitude, ...}, ...]}`
- **Coords:** native WGS84 (Map_Latitude/Map_Longitude as strings).
- **Auth:** none.
- **Sample row (verified):** `Estate_Name="Tak Long Estate", District_Name="Kowloon City", Map_Latitude="22.330105", Map_Longitude="114.2031", No_of_Rental_Flats="8 200 as at 31.12.2025"` — note the numeric-with-status-suffix formatting.

This does **not** fit the generic `csdi.query_features` tool. To go live on HKHA estates we need a small dedicated tool (`housing.fetch_live_estates`) that hits the Housing Authority API and normalises the string numerics. Tracked as a v0.3.4+ follow-up.

## Verification log

| Endpoint | Operation | Result |
|---|---|---|
| basketball courts | `?f=json` (metadata) | 200, 35 fields, wkid=102140, outSR=4326 supported |
| basketball courts | `/query` 3 rows `outSR=4326` | 200, WGS84 geometry, names correct |
| swimming pools | `?f=json` (metadata) | 200, 31 fields, CamelCase naming |
| swimming pools | `/query` 2 rows `outSR=4326` | 200, WGS84 geometry, "Tuen Mun Swimming Pool" |
| HKHA estates (Housing Authority native) | `GET ?format=json` | 200, `{data: [...]}`, WGS84 native |

All five confirmed on 2026-04-23 from the Mac / US network path. CSDI geoportal serves CDN-cached responses; expect ~300–600ms over the public internet.
