# Deployment — Tailscale Serve

**Version:** v0.3.1 · 2026-04-23
**Default posture:** Tailscale-only, no public Funnel, no PII persistence.

---

## Runtime topology

```
┌─ Robot / dev laptop (tailnet) ─┐      ┌─ Mac Studio (tailnet) ─────────┐
│                                │      │                                │
│  WebSocket client ─────────────┼──────┼─> Agent service (FastAPI :8080)│
│  TTS engine                    │      │    └─> LM Studio :1234 (local) │
│                                │      │    └─> SQLite WAL session file │
│                                │      │    └─> KMB stop cache (in-mem) │
│                                │      │                                │
└────────────────────────────────┘      └────────────────────────────────┘
                │                                    │
                └────── Tailscale tailnet (WireGuard) ┘
                                                     │
                                                     └─> data.gov.hk / MTR / KMB / Citybus / HKO / EPD / ALS over public internet
```

## Prerequisites on the Mac Studio

- macOS with Python 3.12 available (or let `uv` install it).
- Tailscale signed in on the **Earnest Design Lab** tailnet as `earnests-mac-studio`.
- LM Studio running with `openai/gpt-oss-120b` loaded and the OpenAI-compatible server started on `:1234`.
- `uv` + `git` + `just` (optional but handy).

Verify LM Studio is up:

```bash
curl -sS http://earnests-mac-studio.taila366aa.ts.net:1234/v1/models
```

### Recommended LM Studio tuning

These are set in the LM Studio UI (Chat or Server tab → model gear ⚙️). None require code changes.

- **Speculative decoding.** Pair `gpt-oss-120b` with `gpt-oss-20b` as the draft model. Expected saving: ~300–800 ms per synthesis hop on the Mac Studio. Covered in `docs/audit/03_enhancements.md` §1.
- **Flash attention.** Enable on Metal; LM Studio 0.3+ supports it for `gpt-oss-*`. Drops first-token latency by 10–20 % on our workload.
- **Keep model loaded.** Set "Keep model loaded after request" so the KV cache isn't evicted between idle turns. The agent passes `user=session_id` on every call, which llama.cpp / LM Studio routes to a per-slot KV cache.

## Install + run

```bash
git clone https://github.com/regitxx/SmartCityIntegration-SRLab.git
cd SmartCityIntegration-SRLab
cp .env.example .env           # adjust BIND_HOST / BIND_PORT if needed
uv sync --extra dev
uv run python -m scripts.llm_ping   # smoke test
uv run uvicorn smcity.app:app --host 127.0.0.1 --port 8080
```

The service binds to `127.0.0.1` by default — it's only reachable via `tailscale serve` (below), not the public internet.

## Expose over Tailscale Serve (recommended)

Serve publishes the service only to devices on the tailnet. No public URL, no Funnel.

```bash
# On the Mac Studio:
tailscale serve --https=443 localhost:8080
# …or listen on a different path/port:
tailscale serve --https=8443 --set-path=/smcity localhost:8080
```

Consumers (robot / dev laptop) then hit:

```
https://earnests-mac-studio.taila366aa.ts.net/
```

…with the usual Tailscale MagicDNS. The Tailscale TLS cert is auto-issued; no Let's Encrypt / manual cert management needed.

### Stop Serve

```bash
tailscale serve --https=443 off
```

## Expose publicly (Funnel — not default)

Only turn this on if an external demo needs the URL. This opens the service to the internet.

```bash
tailscale funnel --https=443 localhost:8080
```

Before enabling:
- Double-check `PII_REDACT_AT_INGRESS=true` in `.env`.
- Consider adding an API-key header check at the FastAPI layer (not in v0.1).
- Monitor the tool-trace log for abuse; LM Studio is a real compute resource.

## Managed deployment (future)

- **systemd-equivalent on macOS:** `launchd` plist with `KeepAlive=true`. Sample at `deploy/launchd/com.labsrl.smcity.plist` (not yet committed; add in v0.2).
- **Container:** not yet — `uv run uvicorn` is the shipped runtime. Dockerfile is a v0.2 candidate.
- **Logs:** `structlog` JSON via stdout. Tail: `uvicorn.out 2>&1 | jq`. Add Langfuse in v0.2 for trace UX.

---

## Threat model

### In-scope for v0.1

| Threat | Mitigation |
|---|---|
| Adversarial chat input attempting SSRF / command injection | Tool dispatcher validates args through pydantic; no string interpolation into upstream URLs; all tool handlers use typed clients. |
| Hallucinated facts (wrong bus stop, invented station) | Every factual claim comes from a tool call. Tools return structured data; the LLM must cite. |
| Prompt-injection via tool output pulling LLM into wrong language | `language_stick_reminder` injected after tool results; `_maybe_polish` enforces the target language deterministically for Cantonese. |
| PII leakage in session storage | `redact_pii()` at ingress (HK phone + HKID); no PII persists. |
| Excessive resource use from any one session | KMB stop catalog fetched once per process; per-tool TTL cache; per-request timeouts on every upstream HTTP call. |
| Public exposure of the LLM to the internet | Default to Tailscale-only; Funnel is opt-in. |

### Hardened in v0.3.1 (was out of scope for v0.1)

- **WebSocket origin allow-list.** `/ws/{session_id}` rejects cross-origin upgrades unless the Origin matches the Host header or is listed in `WS_ALLOWED_ORIGINS`. Set `WS_ALLOWED_ORIGINS=*` to disable (not recommended off-tailnet).
- **Per-session rate limit.** Token-bucket in `smcity.ratelimit`: `RATE_PER_MIN=30` refill + `RATE_BURST=10` by default. The WS path returns `{"type":"error"}` with `retry_after_s`; the HTTP path returns `429` with `Retry-After`.
- **`session_id` regex guard.** `^[A-Za-z0-9_.-]{1,64}$` — enforced on WebSocket connect, in the `TurnRequest` pydantic model, and at every SQLite read/write.
- **SQLite file perms.** `data/sessions.sqlite3` (and its `-wal`/`-shm`/`-journal` shards) are `chmod 600` on creation.
- **Structured tool dispatch log.** Every tool call emits a `tool_call` structlog record with `name`, `status`, `latency_ms`, `cached`, `session_id`. Pipe stdout to your preferred ingest.

### Out of scope

- **Multi-tenant auth.** There is no user model; the service assumes the tailnet itself is the trust boundary.
- **Tamper-evident audit log.** The tool-trace is emitted via structlog but not signed / persisted durably.
- **TLS pinning** for data.gov.hk upstreams. We trust OS certificate store.

### Things we explicitly don't do

- We do not proxy raw LLM completions to the user. All replies are tool-grounded except for canned chitchat and meta prompts.
- We do not claim to look up personal HKHA applications. `housing.get_estate_info` hard-codes a redirect to the official portal in its tool description.
- We do not attempt medical / legal / financial advice. The system prompt caps scope to city info.

---

## Upgrading

```bash
git pull --ff-only
uv sync --extra dev
# Restart uvicorn:
pkill -f 'uvicorn smcity.app:app'
uv run uvicorn smcity.app:app --host 127.0.0.1 --port 8080 &
```

Database migrations are not needed in v0.1 — the single-table SQLite schema is created idempotently on startup.

## Backups

Sessions live in `data/sessions.sqlite3`. Back up with a simple copy while the service is idle, or `sqlite3 data/sessions.sqlite3 .dump > backup.sql`. In v0.1 there is no irreplaceable data — all session state is ephemeral and PII-redacted.
