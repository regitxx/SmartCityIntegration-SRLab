# OpenTripPlanner 2 sidecar — setup + operation

**Version target:** OTP 2.6.0
**Integrated via:** `smcity.tools.otp2.PLAN_MULTIMODAL_JOURNEY_TOOL`
**Endpoint the agent expects:** `http://127.0.0.1:8080/otp/routers/default/plan`

The hand-rolled MTR-only planner (`transport.plan_simple_route`) handles
the bulk of HK user queries. OTP2 adds the missing capability: true
multimodal planning across bus + rail + ferry + minibus with live
timetable awareness. When the sidecar is down the agent falls back
transparently — `plan_multimodal_journey` raises a clean upstream error
and the LLM is prompted to use the simple planner instead.

---

## One-time setup (~15 min + graph build)

You need:
- Docker Engine (or Docker Desktop) — Linux/macOS/Windows fine
- ~8 GB free RAM for the running container
- ~4 GB disk for graph + GTFS feeds + OSM extract

### 1. Collect the GTFS + OSM inputs

Put everything into `otp/data/`:

```
otp/data/
├── hong-kong.osm.pbf          # OSM extract (see below)
├── mtr-gtfs.zip               # MTR GTFS
├── kmb-gtfs.zip               # KMB GTFS
├── citybus-gtfs.zip           # Citybus (CTB) GTFS
├── gmb-gtfs.zip               # Green minibus GTFS (optional)
└── build-config.json          # OTP build tweaks (see below)
```

Sources (as of 2026):

| Input | URL | Notes |
|---|---|---|
| HK OSM extract | `https://download.geofabrik.de/asia/china.html` or `https://download.bbbike.org/osm/bbbike/HongKong/` | bbbike's HK extract is ~60 MB (smaller) vs china's 1+ GB (larger) |
| MTR GTFS | `https://opendata.mtr.com.hk/data/mtr_lines_fares.csv` + data.gov.hk GTFS mirror | MTR publishes CSVs; GTFS wrappers live at various mirrors — check data.gov.hk "GTFS" search |
| KMB GTFS | `https://data.etabus.gov.hk/v1/gtfs/KMB/` | JSON route/stop APIs exist; GTFS exports from community mirrors |
| Citybus GTFS | `https://rt.data.gov.hk/v2/transport/citybus/` | same as KMB — JSON + community GTFS |
| GMB GTFS | data.gov.hk search "green minibus GTFS" | sparser coverage; optional |

> **Heads-up:** HK transit GTFS is NOT all directly served by the agencies. Some feeds are community-maintained. Verify freshness before a production run — if a zip is older than a month the agent will cheerfully plan against stale timetables.

### 2. Minimal `build-config.json`

```json
{
  "transitFeeds": [
    { "type": "gtfs", "source": "mtr-gtfs.zip" },
    { "type": "gtfs", "source": "kmb-gtfs.zip" },
    { "type": "gtfs", "source": "citybus-gtfs.zip" },
    { "type": "gtfs", "source": "gmb-gtfs.zip" }
  ],
  "osm": [{ "source": "hong-kong.osm.pbf" }],
  "timeZone": "Asia/Hong_Kong"
}
```

### 3. Build the graph (one-off)

```bash
cd otp/
docker compose run --rm otp2 --build --save
```

Expect ~15–30 min on an M-series Mac with 4 GB heap. Output: `otp/data/graph.obj` (gitignored — see root `.gitignore`).

### 4. Start the server

```bash
docker compose up -d
# wait for the healthcheck to pass:
docker compose ps
curl -s http://127.0.0.1:8080/otp/routers/default | jq .polygon
```

The smcity agent picks it up automatically via `OTP2_BASE_URL`
(default `http://127.0.0.1:8080/otp`).

### 5. Smoke-test against the agent

```bash
uv run python - <<'PY'
import asyncio
from smcity.tools import build_default_registry
from smcity.tools.registry import ToolContext

async def main():
    reg = build_default_registry()
    ctx = ToolContext(session_id="otp-smoke")
    res = await reg.dispatch(
        "transport.plan_multimodal_journey",
        {
            "origin_lat": 22.2820, "origin_lng": 114.1582,  # Central
            "destination_lat": 22.3817, "destination_lng": 114.1870,  # Sha Tin
            "modes": ["TRANSIT", "WALK"],
        },
        ctx,
    )
    print(res.status, res.latency_ms, "ms")
    for i, it in enumerate((res.result or {}).get("itineraries", []), 1):
        print(f"  [{i}] {it['duration_s']//60} min, {len(it['legs'])} legs")

asyncio.run(main())
PY
```

---

## Updating the graph

HK transit timetables change frequently (bus routes, MTR extensions, ferry schedules). Rebuild monthly, or whenever you notice plans getting stale:

```bash
cd otp/
# refresh the GTFS + OSM inputs in ./data/, then:
docker compose down
docker compose run --rm otp2 --build --save
docker compose up -d
```

---

## Operational notes

- **Memory**: OTP2 wants Xmx4g for a HK-size graph. `docker-compose.yml` sets this via `JAVA_TOOL_OPTIONS`. If you see OOM, bump to 6g.
- **Bind address**: the compose file binds `127.0.0.1:8080`. Loopback-only keeps OTP2 off the tailnet; the agent calls it locally. Change to `0.0.0.0:8080` only if a different host runs smcity.
- **Port conflict**: smcity's uvicorn also defaults to 8080. Run OTP2 on a different port (edit the compose `ports:` line and set `OTP2_BASE_URL=http://127.0.0.1:8081/otp`) or move the agent.
- **Cold start**: the first query after `docker compose up` takes 2–5 s while the graph initialises. Subsequent queries are ~200–600 ms depending on distance.

---

## What this does NOT solve yet

- **Minibus GMB live ETA fusion** — OTP2 plans from scheduled GTFS, not live ETA. Use `transport.get_gmb_eta` for the "when's the next one?" query after the plan.
- **Fare prediction** — OTP2 2.6 emits fare legs only when GTFS `fare_rules.txt` is provided; most HK feeds omit it. Treat fare numbers as best-effort.
- **Cross-harbour ferry inclusion** — depends on the ferry GTFS feed being present in `./data/`.

All three are tracked as potential follow-ups but out of scope for v0.4.4.
