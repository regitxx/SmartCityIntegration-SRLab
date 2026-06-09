# Test cases — one verification query per tool

**Purpose:** quick-fire copy-paste queries you (or a teammate on the tailnet) can drop into the chat UI to verify each of the 27 tools works against live HKSAR open-data APIs.

**Access:**
- Local: <http://127.0.0.1:8080>
- Tailnet (production): <https://smcity-1.taila366aa.ts.net> — tailnet-only, TLS terminated by the Tailscale sidecar

**Prereq:** LM Studio on the Mac Studio must be online with `openai/gpt-oss-120b` loaded. Verify: `curl http://127.0.0.1:8080/health` — `llm_reachable` must be `true`.

All queries listed in both **English** and **Cantonese (繁體)** where natural. Each row names the upstream API the agent will hit live + the expected tool the LLM should pick.

---

## Calibration scoreboard

Headline metrics from the calibrated 200-row sweep (fixed corpus `data/synth/v0.6.0_20260526_sample200_calibration.jsonl`, concurrency 1, judged with `gpt-oss-120b`). Lower is better for `tool_error`/`wrong_tool`/latency; higher for pass rate and score. Full per-release write-ups live in `CHANGELOG.md`.

| metric | v0.7.0 | v0.7.1 | v0.8.0 | target |
|---|---:|---:|---:|---:|
| pass rate | 33.5% | 28.0% | **45.5%** | → higher |
| avg score /10 | 5.72 | 5.88 | **6.99** | → higher |
| `wrong_tool`/100 | 39.5 | 30.0 | **26.0** | → 0 |
| `tool_error`/100 | 4.5 | 27.0 | **4.0** | → 0 |
| latency median | — | 11.3s | **7.1s** | **≤1.5s** (`GOAL.md`) |
| latency p95 | — | 24.7s | **19.2s** | — |

**v0.8.0 (local POI mirror):** removed the live-Overpass 504 confound (0 Overpass calls in the sweep; all 127 `find_poi` served from the mirror), collapsing `tool_error` 27→4 and recovering pass rate to 45.5%. **Latency is the remaining gap** — 7.1s median is ~5× the ≤1.5s goal; the two `gpt-oss-120b` hops (decide + synthesise) now dominate. To reproduce: `FUZZER_MODEL=openai/gpt-oss-120b uv run python -m smcity_fuzz coverage run --questions data/synth/v0.6.0_20260526_sample200_calibration.jsonl --agent-url https://smcity-1.taila366aa.ts.net --concurrency 1 --out logs/coverage_results_<ver>.jsonl` then `… coverage_judge --results … --out …`.

---

## Transport (12 tools)

| Tool | Try this query | Upstream API hit live |
|---|---|---|
| `transport.get_mtr_next_trains` | "Next train from Central station" / "中環站下一班車幾時到？" | rt.data.gov.hk/v1/transport/mtr |
| `transport.get_kmb_eta_by_stop` | "When's the next KMB bus at Sheung Wan MTR?" / "上環港鐵站下一班九巴幾時到？" | data.etabus.gov.hk |
| `transport.get_kmb_eta_by_route_stop` | "KMB route 1A ETA at Mong Kok stop" / "九巴 1A 喺旺角嗰個站幾時到？" | data.etabus.gov.hk |
| `transport.get_citybus_eta_by_route_stop` | "Citybus 1 at stop 001028" / "城巴 1 喺 001028 站幾時到？" | rt.data.gov.hk/v2/transport/citybus |
| `transport.get_citybus_route_stops` | "List Citybus route 1 stops" / "城巴 1 路經過邊啲站？" | rt.data.gov.hk/v2/transport/citybus |
| `transport.get_gmb_eta` | "Green minibus 25M ETA in Wan Chai" / "灣仔 25M 綠 van 幾時到？" | data.etagmb.gov.hk |
| `transport.find_stops_near_point` | "Bus stops near 22.2863, 114.1515" / "上環港鐵站附近有邊啲巴士站？" | (local catalog + KMB stop API) |
| `transport.find_stops_by_name` | "Find all stops named Sheung Wan" / "搵下叫『上環』嘅站" | (local catalog + KMB stop API) |
| `transport.plan_simple_route` | "MTR from Sheung Wan to Sha Tin" / "搭 MTR 由上環去沙田" | local Dijkstra over MTR topology |
| `transport.plan_walking_route` | "Walk from Central to Wan Chai" / "由中環行去灣仔" | (local pace model + ALS for geocoding) |
| `transport.plan_journey` | "How do I get from Tsim Sha Tsui to Causeway Bay?" / "由尖沙咀去銅鑼灣點去？" | composes walk + MTR (no taxi — transit/walk only) |
| `transport.plan_multimodal_journey` | "Multimodal route Central → Sha Tin via bus + MTR" — **needs OTP2 sidecar running**, see `otp/README.md` | local OTP2 docker sidecar |

## Context — weather / air / warnings (4 tools)

| Tool | Try this query | Upstream API hit live |
|---|---|---|
| `context.get_current_weather` | "What's the weather right now?" / "而家天氣點呀？" | data.weather.gov.hk · `rhrread` |
| `context.get_active_warnings` | "Any typhoon warning?" / "而家有冇颱風警告？" | data.weather.gov.hk · `warnsum` |
| `context.get_9day_forecast` | "Will it rain this weekend?" / "今個週末會唔會落雨？" | data.weather.gov.hk · `fnd` |
| `context.get_aqhi` | "Air quality in Central" / "中環空氣質素點？" | aqhi.gov.hk RSS feed |

## Facility (2 tools, live CSDI)

| Tool | Try this query | Upstream API hit live |
|---|---|---|
| `facility.find_nearby_courts` | "Free basketball courts in Sha Tin" / "沙田有冇免費籃球場？" | portal.csdi.gov.hk · LCSD courts FeatureServer (305 venues) |
| `facility.find_nearby_pools` | "Swimming pools in Wan Chai" / "灣仔有邊啲公眾泳池？" | portal.csdi.gov.hk · LCSD pools FeatureServer (46 pools) |

## Housing (2 tools, live HKHA API)

| Tool | Try this query | Upstream API hit live |
|---|---|---|
| `housing.get_estate_info` | "Tell me about Choi Hung Estate" / "彩虹邨有幾多座？" | data.housingauthority.gov.hk (241 estates) |
| `housing.list_estates_in_district` | "All HKHA estates in Sham Shui Po" / "深水埗有邊啲公屋？" | data.housingauthority.gov.hk |

> **Safety guard:** "Can you check my housing application status?" → must REDIRECT to <https://www.housingauthority.gov.hk/en/flat-application/>; never claim to check.

## Geo (2 tools)

| Tool | Try this query | Upstream API hit live |
|---|---|---|
| `geo.address_lookup` | "Where is 50 Lung Mun Road, Tuen Mun?" / "屯門龍門路 50 號喺邊？" | www.als.gov.hk |
| `geo.search_osm_pois` | see the 30-category list below | overpass-api.de |

### `geo.search_osm_pois` — 30 categories (xlsx S514-S549)

| Category | Try this query | Notes |
|---|---|---|
| `convenience_store` | "Nearest 7-Eleven to me" / "附近邊度有 7-11？" | S514 |
| `supermarket` | "Supermarkets in Mong Kok" / "旺角有邊啲超市？" | S515 |
| `public_toilet` | "Nearest public toilet" / "最近嘅公廁喺邊？" | S516 |
| `place_of_worship` | "Temples near Wan Chai" / "灣仔附近嘅廟" | S517 |
| `mtr_station_entrance` | "Central MTR exits" / "中環站出入口" | S518 |
| `recycling_location` | "Where to recycle plastic?" / "邊度可以回收膠樽？" | S519 |
| `veterinarian` | "Vets in Tai Po" / "大埔嘅獸醫" | S520 |
| `hardware_store` | "Hardware store near Causeway Bay" / "銅鑼灣五金舖" | S521 |
| `public_elevator` | "Public elevators in Central" / "中環嘅公共電梯" | S522 |
| `hairdresser` | "Hair salons near Mong Kok" / "旺角嘅髮型屋" | S523 |
| `clothes_shop` | "Clothing stores in Tsim Sha Tsui" / "尖沙咀嘅服裝店" | S524 |
| `electronics_shop` | "Electronics in Sham Shui Po" / "深水埗嘅電子用品舖" | S525 |
| `department_store` | "Department stores in Causeway Bay" / "銅鑼灣嘅百貨公司" | S526 |
| `variety_store` | "Daiso / variety stores near me" / "附近嘅雜貨舖" | S527 |
| `houseware_shop` | "Houseware near Yuen Long" / "元朗嘅家居用品舖" | S528 |
| `beauty_shop` | "Beauty stores in Mong Kok" / "旺角嘅美容店" | S529 |
| `optician` | "Opticians in Central" / "中環嘅眼鏡舖" | S530 |
| `shoe_shop` | "Shoe stores in Tsim Sha Tsui" / "尖沙咀嘅鞋舖" | S531 |
| `greengrocer` | "Wet market vegetables in Wan Chai" / "灣仔賣菜檔" | S532 |
| `marketplace` | "Wet markets near North Point" / "北角嘅街市" | S533 |
| `bookstore` | "Bookstores in Causeway Bay" / "銅鑼灣嘅書店" | S534 |
| `drinking_water` | "Drinking water fountains in Victoria Park" / "維園邊度有飲水機？" | S535 |
| `laundry` | "Laundromats in Sham Shui Po" / "深水埗嘅洗衣店" | S536 |
| `government_office` | "Government offices in Wan Chai" / "灣仔政府部門" | S537 |
| `kiosk` | "Kiosks at Causeway Bay MTR" / "銅鑼灣地鐵嘅小食亭" | S538 |
| `dentist` | "Dentists near Mong Kok" / "旺角嘅牙醫" | S541 |
| `bookmaker` | "Jockey Club betting branches" / "馬會投注站" | S542 |
| `bench` | "Public benches in Central Pier" / "中環碼頭嘅座椅" | S543 |
| `shelter` | "Rain shelters along Hennessy Road" / "軒尼詩道嘅避雨亭" | S544 |
| `handrail` | "Handrails on the Mid-Levels escalator" / "半山扶手電梯嘅扶手" | S549 |

## CSDI (1 generic tool)

| Tool | Try this query | Notes |
|---|---|---|
| `csdi.query_features` | rarely user-facing; agent uses it for advanced ArcGIS queries on registered datasets (`lcsd_basketball_courts`, `lcsd_swimming_pools`) | portal.csdi.gov.hk |

## Meta (3 tools)

| Tool | Try this query | Behavior |
|---|---|---|
| `meta.ask_user` | "Get me there" (no destination) | Agent should ask "where to?" — should NOT make one up |
| `meta.what_languages_are_supported` | "What languages do you speak?" / "支援邊啲語言？" | Should call the meta tool; not hard-code an answer |
| `meta.forget_me` | "Forget everything I said" / "唔記得我講過嘅嘢" / "delete my data" | Wipes the session row + acknowledges |

---

## Smoke-check checklist

Quickest single command to validate the whole stack is healthy:

```bash
curl -s http://127.0.0.1:8080/health | jq .
# expect: {"status":"ok", "llm_reachable":true, "llm_model":"openai/gpt-oss-120b", "version":"0.4.7"}
```

If `llm_reachable` is `false`, the agent is fine but LM Studio isn't reachable. Restart LM Studio on the Mac Studio, confirm 120b is loaded, then re-curl.

To verify **upstreams** without going through the agent:

```bash
# basketball courts — should return 200, real data
curl -s "https://portal.csdi.gov.hk/server/rest/services/common/lcsd_rcd_1629267205215_38105/FeatureServer/0/query?where=1=1&outFields=NAME_EN&f=json&resultRecordCount=1" | python3 -m json.tool | head -20

# weather — should return 200, real temperature
curl -s "https://data.weather.gov.hk/weatherAPI/opendata/weather.php?dataType=rhrread&lang=en" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('temperature',{}).get('data',[None])[0])"

# HKHA estates — should return 241 rows
curl -s "https://data.housingauthority.gov.hk/psi/rest/export/prh-estates?format=json" | python3 -c "import json,sys;print(len(json.load(sys.stdin)['data']))"
```

## Adversarial pass (run the fuzzer)

The fuzzer drives all 27 tools through 5 personas × 4 languages with an LLM-as-judge. Faster than manually copy-pasting 27 × 4 queries.

```bash
# In a separate terminal (agent must be running):
uv run python -m smcity_fuzz run --mode ws --turns 20 --concurrency 1 --seed 42
uv run python -m smcity_fuzz export --out handoff/$(date +%Y-%m-%d).md --max-failures 30
```
