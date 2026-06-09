# smcity on the Mac Studio — Docker + Tailscale + zero-downtime deploy

Runs the agent as **two replicas behind nginx** so a code update never drops a request, plus the Tailscale sidecar that terminates HTTPS at `https://smcity.<tailnet>.ts.net/`.

```
            ┌──────────────────────────── Mac Studio host ────────────────────────────┐
            │                                                                         │
boss ─►     │   ┌────────────────┐   ┌──────────────┐   ┌───────────────────────┐    │
(HTTPS)   ──┼──►│ smcity-        │──►│ smcity-      │──►│ smcity-agent-blue     │    │
            │   │  tailscale     │   │  router      │   │   uvicorn :8080       │    │
            │   │  terminates    │   │  nginx       │   │   FastAPI · 27 tools  │    │
            │   │  TLS at        │   │  load-       │   │                       │    │
            │   │  *.ts.net      │   │  balancer    │   ├───────────────────────┤    │
            │   │                │   │  + retry     │──►│ smcity-agent-green    │    │
            │   └────────────────┘   └──────────────┘   │   uvicorn :8080       │    │
            │                                           │   FastAPI · 27 tools  │    │
            │                                           └─────────┬─────────────┘    │
            │                                                     │                  │
            │              all replicas reach LM Studio ◄─────────┘                  │
            │              on the host via host.docker.internal :1234                │
            │                                                                        │
            │              all replicas export OTel spans ─► Phoenix Arize           │
            │              (https://phoenix.sustainer.ai, project=smcity)            │
            └────────────────────────────────────────────────────────────────────────┘
```

**Zero-downtime model:** the nginx router has both blue and green as upstreams. When `deploy.sh` rebuilds and recreates blue, nginx automatically routes all traffic to green for the ~10s blue is restarting (and vice versa). Clients see no 5xx, no dropped WebSocket frames.

## Prereqs on the Mac Studio (one-time)

1. **Docker Desktop for Mac** installed and running (`docker info` succeeds).
2. **LM Studio** running with `openai/gpt-oss-120b` loaded.
3. **`host.docker.internal` reachable from containers** — built-in on Docker Desktop for Mac.
4. **Tailscale tailnet ready** — the deploy registers a new node called `smcity` on Earnest Design Lab's tailnet.

## One-time setup

```bash
mkdir -p ~/srv && cd ~/srv
git clone https://github.com/regitxx/SmartCityIntegration.git smcity   # or rsync
cd smcity/deploy

# Fill in .env: Tailscale auth key + Phoenix API key.
cp .env.example .env
nano .env

# Bring up the full stack: blue + green + router + tailscale.
docker compose up -d --build

# Wait for everything healthy (~30 s):
docker compose ps
```

`docker compose ps` should show four containers, all `Up … (healthy)`:

| name                  | role                       |
|-----------------------|----------------------------|
| smcity-agent-blue     | agent replica #1           |
| smcity-agent-green    | agent replica #2           |
| smcity-router         | nginx load-balancer        |
| smcity-tailscale      | tailnet sidecar + TLS      |

## Subsequent code updates — `./deploy.sh`

```bash
cd ~/srv/smcity/deploy
# pull / rsync the new code first, then:
./deploy.sh
```

The script:
1. Builds a new image with the current source.
2. Recreates `smcity-agent-blue` — green keeps serving every request.
3. Waits for blue's healthcheck to go green.
4. Recreates `smcity-agent-green` — blue serves every request.
5. Reloads the nginx config (clears any cached unhealthy markers).

Use `./deploy.sh --no-build` when the only change is a config tweak (e.g. flipping AllowFunnel) and you want to roll the replicas without rebuilding the image.

## Phoenix Arize tracing

Spans appear at <https://phoenix.sustainer.ai/projects/smcity>. The agent emits:

- `smcity.turn` per user request, with `session.id`, `user.text`, `reply.text`, `tool_count`, `detected_lang`.
- `tool.<name>` per tool call, with `tool.args`, `tool.result` (truncated to 8 KB), `tool.status`, `tool.latency_ms`, `tool.cached`.
- `llm.chat` per OpenAI-compatible call (auto-instrumented by openinference) with model, messages, completion, token counts.
- Outbound httpx calls (data.gov.hk, OSM Nominatim, ALS, MTR realtime, …) auto-instrumented with method/URL/status/duration.

Set `PHOENIX_DISABLE=1` in `.env` to suppress the OTLP exporter (e.g. if Phoenix is briefly unavailable). Spans are still produced in-process — only the network export is disabled.

## Smoke tests

```bash
# Through the router (internal):
docker compose exec smcity-router wget -qO- http://localhost:8080/health

# Through the Tailscale URL (tailnet-only; the node is `smcity-1`, not `smcity`):
curl https://smcity-1.taila366aa.ts.net/health
```

Expected: `{"status":"ok","llm_reachable":true,"llm_model":"openai/gpt-oss-120b","version":"0.4.14"}`

## Operating commands

```bash
docker compose logs -f smcity-agent-blue       # tail one replica
docker compose logs -f smcity-router            # nginx access + error log
docker compose restart smcity-agent-blue        # restart just one replica
docker compose down                             # tear everything down
docker compose ps                               # current status
```

## Troubleshooting

| Symptom                                                | Likely cause                                                                                  |
|---------------------------------------------------------|-----------------------------------------------------------------------------------------------|
| `health` returns `llm_reachable: false`                | LM Studio not running on the host, or no model loaded (`lms ps` to verify).                   |
| Sidecar logs "no DERP relay"                           | Tailscale auth key expired/used. Regenerate at https://login.tailscale.com/admin/settings/keys. |
| `502 Bad Gateway` from the URL                         | Both replicas down. `docker compose ps` to inspect; usually fixed by `docker compose up -d`.  |
| Phoenix project shows no spans                         | `PHOENIX_API_KEY` empty or wrong, or `PHOENIX_COLLECTOR_ENDPOINT` missing the `https://` scheme. |
