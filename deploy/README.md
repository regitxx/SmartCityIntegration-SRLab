# smcity on the Mac Studio — Docker + Tailscale sidecar

Deploys the agent as two containers on the Mac Studio so the demo URL keeps working when the Macbook sleeps. The Tailscale sidecar terminates HTTPS and proxies to the agent over the docker bridge — no public IP, no router config, no cert plumbing.

```
              ┌──────────────────── Mac Studio host ────────────────────┐
              │                                                         │
   boss ─►    │   ┌──────────────────────┐    ┌──────────────────────┐  │
   (HTTPS)   ─┼──►│ smcity-tailscale     │───►│ smcity-agent         │  │
              │   │  sidecar             │    │   uvicorn :8080      │  │
              │   │  joins the tailnet,  │    │   FastAPI · 27 tools │  │
              │   │  terminates TLS at   │    │                      │  │
              │   │  https://<host>.ts.net│   │  reaches LM Studio   │  │
              │   │  → proxies to agent  │    │  on the host via     │  │
              │   └──────────────────────┘    │  host.docker.internal│  │
              │                               └─────────┬─────────────┘ │
              │                                         │               │
              │                                         ▼               │
              │                          LM Studio :1234 (on host)      │
              │                              gpt-oss-120b               │
              └─────────────────────────────────────────────────────────┘
```

## Prereqs on the Mac Studio (one-time)

1. **Docker Desktop for Mac** installed and running. (Check with `docker info`.)
2. **LM Studio** running with `openai/gpt-oss-120b` loaded.
3. **`host.docker.internal` reachable from containers** — this is built-in on Docker Desktop for Mac. Verify:
   ```bash
   docker run --rm alpine sh -c "apk add curl >/dev/null; curl -s host.docker.internal:1234/v1/models | head -c 200"
   ```
4. **Tailscale tailnet ready** — the deploy will register a new node called `smcity`. If Funnel-public access is wanted later, enable Funnel for the tailnet at <https://login.tailscale.com/admin/dns> and edit `serveconfig.json` to flip `AllowFunnel: true`.

## One-time setup

```bash
# 1. Copy the repo to the Mac Studio (anywhere; here we use ~/srv/smcity):
mkdir -p ~/srv && cd ~/srv
git clone https://github.com/regitxx/SmartCityIntegration-SRLab.git smcity
cd smcity/deploy

# 2. Drop your Tailscale auth key into .env:
cp .env.example .env
$EDITOR .env   # paste TS_AUTHKEY=tskey-auth-...

# 3. Build + start the stack:
docker compose up -d --build
```

The first build takes ~3–5 min (uv resolves deps, builds the wheel). Subsequent rebuilds are cached.

## What success looks like

```bash
docker compose ps
# Both services should be "running" / "healthy"
#   NAME              IMAGE                          STATUS
#   smcity-agent      smcity:0.4.10                  Up · healthy
#   smcity-tailscale  tailscale/tailscale:latest     Up

docker compose logs smcity-tailscale | grep -E "magicdns|listening|https"
# Should show: "tailscaled is running" and a HTTPS URL line
```

The agent is then reachable at:

- Inside the tailnet: <https://smcity.YOUR-TAILNET.ts.net/>
- The exact URL is printed in the sidecar logs (look for `magicdns:`).

Quick smoke test from any tailnet machine (or the Mac Studio itself):

```bash
curl -s https://smcity.YOUR-TAILNET.ts.net/health | jq .
# {"status":"ok","llm_reachable":true,"llm_model":"openai/gpt-oss-120b","version":"0.4.10"}

# Open in a browser to use the chat UI:
open https://smcity.YOUR-TAILNET.ts.net/
```

## Sharing with the boss

**Path A — tailnet member (recommended for one-on-one demos):**
1. Invite the boss as a user on the tailnet at <https://login.tailscale.com/admin/users>.
2. He installs Tailscale, accepts the invite, hits the same `smcity.YOUR-TAILNET.ts.net` URL.
3. Revoke his access at any time.

**Path B — public via Funnel (no Tailscale install needed on his side):**
1. Enable Funnel for the tailnet at <https://login.tailscale.com/admin/dns> (one-click).
2. Edit `serveconfig.json` → change `"AllowFunnel": false` to `"AllowFunnel": true` for the `:443` entry.
3. `docker compose restart smcity-tailscale`.
4. The same URL is now reachable from the public internet (anyone with the URL can access). The agent's per-session rate limit + WS origin guard + PII redaction stay in place.

## Operating it

```bash
# Tail live logs
docker compose logs -f

# Restart just the agent (e.g. after a git pull)
git pull
docker compose up -d --build smcity-agent

# Stop everything (sidecar will leave the tailnet within ~30s)
docker compose down

# Stop + wipe persisted session DB (rare)
docker compose down -v
```

## When LM Studio isn't loaded

If `gpt-oss-120b` isn't loaded in LM Studio on the host, `/health` will report `llm_reachable: false` and every tool-using turn falls back to `"(LM Studio unreachable — check the Mac Studio)"`. The agent itself stays up.

Load remotely from any tailnet machine without touching the Mac Studio:

```bash
# Python SDK approach (uses the LM Studio websocket)
uv run --with lmstudio python -c "
import lmstudio as lms
with lms.Client(api_host='127.0.0.1:1234') as c:
    c.llm.load_new_instance('openai/gpt-oss-120b')
    print('loaded:', [m.identifier for m in c.llm.list_loaded()])
"
```

Or `lms load openai/gpt-oss-120b` directly on the Mac Studio.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `tailscaled exited code 1` in sidecar logs | Auth key already used or wrong tags | Generate a fresh key, paste into `.env`, `docker compose up -d --force-recreate smcity-tailscale` |
| Sidecar healthy but agent `health: starting` then unhealthy | LM Studio not loaded, agent retries forever | Load model on host (see above) |
| `host.docker.internal: name does not resolve` | Not on Docker Desktop (Linux Docker daemon) | The compose already sets `extra_hosts: host-gateway` for this — should work; if it doesn't, replace with the host IP |
| `Funnel not enabled on tailnet` | One-time admin click missing | <https://login.tailscale.com/f/funnel?node=...> (URL appears in sidecar logs) |
| Boss says cert is invalid | Browser cached an old `*.ts.net` cert | Hard refresh; Tailscale auto-issues a real Let's-Encrypt cert per hostname |
| Agent OOMs | Default is single worker; the LLM is on the host so the container is small — under 200 MB. If you see OOMs the LLM is overloading the host | Reduce `LLM_TIMEOUT_S` or use a smaller model |

## What's NOT in this deploy yet

- **OpenTripPlanner 2 sidecar** (`otp/`) for true multimodal (bus + ferry + minibus). Separate docker-compose, requires GTFS feeds — see `otp/README.md`.
- **The adversarial fuzzer** (`smcity_fuzz/`). Run that from your laptop against the deployed URL; no need to run in production.
- **Persistent log shipping**. Container logs live in `docker logs`; if you want them off-host, add a logging driver (loki, fluent-bit, etc.) — out of scope for the demo.
