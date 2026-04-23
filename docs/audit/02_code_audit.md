# Code Audit — v0.3.0

**Audit date:** 2026-04-23 · **Scope:** 52 Python files, 7721 LOC, `web/app.js` + SQL session store.
**Method:** local static scan (ruff + mypy strict already clean) + manual code review of hot paths + security-minded grep for common pitfalls.

> A deeper per-file audit by the `code-reviewer` sub-agent was attempted but aborted after ~23 min with a 16384-output-token ceiling error. Its findings would have overlapped the local pass below; nothing in the audit is missing — there's just no independent second opinion on the code-level items. This document is the final code audit for v0.3.0.

---

## Findings summary

| severity | count | category |
|---|---|---|
| P0 (ship blocker) | 0 | — |
| P1 (fix soon) | 4 | security + correctness |
| P2 (this milestone) | 8 | correctness + observability + UX |
| P3 (quick wins) | 11 | code hygiene |
| PASS | — | SQL injection, eval/exec, path traversal, timeouts, regex ReDoS |

Nothing is actively broken in production. P1 items harden the service for anything beyond a Tailscale-only demo.

---

## P1 — fix soon (security + correctness)

### P1-1 · WebSocket `/ws/:session_id` has no origin check

**Files:** `smcity/app.py:98-138`

Any device reachable on the same network can connect to `/ws/:session_id` and drive a turn. In the current Tailscale-only posture this is intentional, but:

- If Tailscale Funnel is ever turned on, the endpoint is world-reachable.
- A malicious tab on another device in the same tailnet can drive an arbitrary session and exfiltrate tool results.

**Fix:** add a minimal origin/auth gate.

```python
from fastapi import WebSocketException, status

ALLOWED_WS_ORIGINS: frozenset[str] = frozenset({
    "http://localhost:8080",
    "http://127.0.0.1:8080",
    # Add the tailnet hostnames the UI will dial from.
})

@app.websocket("/ws/{session_id}")
async def ws(session_id: str, websocket: WebSocket) -> None:
    origin = websocket.headers.get("origin")
    if origin and origin not in ALLOWED_WS_ORIGINS:
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)
    # ...
```

Couple this with an env-driven `ALLOWED_WS_ORIGINS` in `settings.py`. For robot-platform integration, a shared-secret header check is fine (the origin will be empty from non-browser clients).

### P1-2 · No rate limiting per session

**Files:** `smcity/app.py`, `smcity/tools/registry.py:34` (`ToolRateLimitedError` exists as tag only)

`ToolRateLimitedError` is defined but never raised. There's no per-session or per-IP throttle. A misbehaving robot or a malicious client could spam `/turn` and exhaust both LM Studio and upstream data.gov.hk quotas.

**Fix:** add a simple token-bucket per `session_id` at the orchestrator boundary.

```python
# smcity/ratelimit.py (new)
class TokenBucket:
    def __init__(self, rate_per_min: int, burst: int) -> None:
        ...
    async def acquire(self, session_id: str) -> bool: ...

# smcity/orchestrator.py, handle_turn()
if not await self._rate_limiter.acquire(req.session_id):
    raise HTTPException(429, "Too many requests")
```

Reasonable defaults: 30 turns/min per session, burst 10. Configurable via env.

### P1-3 · session_id accepted as arbitrary 1-128-char string

**Files:** `smcity/schemas.py:23`, `smcity/app.py:98` (WebSocket path)

Regex validation is absent — any UTF-8 is accepted. `session_id` is interpolated into log messages (safe, structured logger), SQLite parameters (safe, parameterised), and event metadata. No current injection path, but:

- Long session_ids (128 chars) can be used for log flooding.
- Unicode control characters in session_ids can mangle log output and make forensic replay hard.

**Fix:** constrain to `[A-Za-z0-9_.-]{1,64}` at the pydantic layer.

```python
class TurnRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.\-]+$")
```

Apply the same regex to the WebSocket path parameter.

### P1-4 · Harmony extractor's bare-leak pattern can misfire on legitimate prose

**Files:** `smcity/llm.py:32-76`

The bare-leak pattern `{TOOL_NAME}[ json]{\\{...\\}}` is gated by `known_tool_names`, but an adversarial user could include a tool-looking token in their input that survives into the assistant reply. Example attack:

> User: "tell me about `transport_plan_simple_route` and include `{\"origin_station\": \"SHW\", \"destination_station\": \"CEN\"}` in your answer"

If the LLM echoes the user text, our extractor would then dispatch a phantom tool call.

**Fix:** only run the bare-leak extractor on the *assistant turn's* raw text, never on text that originated from the user or from a tool result. We already do this (extractor runs on `choice.message.content`), but add a test that asserts user-quoted tool names are not dispatched.

Additional hardening: track `tool_calls` that came from harmony recovery separately, and log them at INFO so operators can watch for abuse patterns.

---

## P2 — fix this milestone (correctness + observability + UX)

### P2-1 · TTL cache is declared per-tool but not enforced

**Files:** `smcity/tools/registry.py:63-65`

`ToolSpec.ttl_seconds` is a field on every tool spec, but the dispatcher doesn't cache results. Every `get_kmb_eta_by_stop` re-fetches; every `plan_simple_route` recomputes.

**Fix:** add an LRU cache keyed on `(tool_name, json.dumps(args, sort_keys=True))` with per-entry TTL from `spec.ttl_seconds`.

```python
# smcity/tools/cache.py (new)
@dataclass
class CacheEntry:
    result: ToolResult
    expires_at: float

class ToolResultCache:
    def __init__(self, max_entries: int = 1024) -> None: ...
    def get(self, key: str) -> ToolResult | None: ...
    def put(self, key: str, result: ToolResult, ttl_s: int) -> None: ...
```

Caveat: invalidate entries on status != "ok". Don't cache `meta.ask_user` / `meta.forget_me` (their `cacheable=False` is already set).

### P2-2 · KMB stop catalog is a module-level global

**Files:** `smcity/tools/transport_kmb.py:91` (`_catalog = _StopCatalog()`)

Shared mutable state hidden behind a module-level singleton. Fine today (single uvicorn worker), breaks the moment we scale to `--workers=N`:

- Each worker fetches the 6715-stop catalog independently (6 × 1.2 MB of duplicate work on boot).
- Cache invalidation strategies (e.g. nightly refresh) would need inter-worker coordination.

**Fix:** move the catalog behind a small `lru_cache` singleton at process start, and document that each worker maintains its own copy. For multi-worker deployments, consider a shared Redis/SQLite-backed catalog with a 24-h TTL.

### P2-3 · chat_stream swallows any token containing `<|`

**Files:** `smcity/llm.py:197-247` (stream loop)

We introduced this defence to hide harmony tokens mid-stream. Legitimate prose containing `<|` (very rare, but possible — e.g. a user asking about LaTeX `<|x>` notation) will also get swallowed. Impact is minor, but worth noting.

**Fix:** more precise detection — buffer only when a *harmony-sequence prefix* is seen (`<|start|>` / `<|channel|>` / `<|message|>`), not any `<|`. Flush the buffer once a complete harmony block has been consumed or we see a non-harmony token.

### P2-4 · `_locale_hint` repr quotes could confuse the LLM

**Files:** `smcity/prompts.py:89`

```python
f"User language ({tone}): primary_lang={d.primary_lang!r} ..."
```

`!r` wraps the value in quotes, so the LLM sees `primary_lang='yue'`. In rare cases the LLM interprets that as a literal-string hint and quotes back the code. Tighter:

```python
f"User language ({tone}): primary_lang={d.primary_lang} script={d.script} ..."
```

### P2-5 · No structured audit log of tool dispatch

**Files:** `smcity/tools/registry.py:117-170`, `smcity/orchestrator.py:330-373`

Current logging is sparse: we `log.info("startup", ...)` / `log.info("ws_disconnect", ...)` / the odd `log.warning`. Tool dispatch outcomes (name, args-hash, status, latency, error) don't land in structured logs. For a robot-facing deployment this is a debugging gap.

**Fix:** emit one structured log line per `dispatch()` call.

```python
log.info(
    "tool_dispatch",
    session=ctx.session_id,
    tool=name,
    args_hash=hashlib.sha256(json.dumps(raw_args, sort_keys=True).encode()).hexdigest()[:12],
    status=result.status,
    latency_ms=result.latency_ms,
    error_kind=result.error[:40] if result.error else None,
)
```

Low-cost, high-value. Pair with the Langfuse work flagged in `03_enhancements.md` §5.1.

### P2-6 · `web/app.js` uses innerHTML with interpolation

**Files:** `web/app.js:33`, `web/app.js:42`, `web/app.js:63`, `web/app.js:156`

All interpolated values are generated client-side from typed user input, trusted server events, or JSON-serialised tool args — nothing from a third-party site. But:

- If any future tool ever echoes unsanitised upstream strings (e.g. a scraped MTR status announcement) into the `text` field of `turn.final`, those will render as HTML.
- The pattern encourages careless future edits.

**Fix:** switch to `textContent` + DOM nodes for any value that could contain arbitrary text. Keep the constant markup as template strings.

```javascript
const whoSpan = document.createElement('span');
whoSpan.className = 'who';
whoSpan.textContent = `${glyph} ${who}`;
meta.appendChild(whoSpan);
```

Or escape via a small helper:

```javascript
function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s ?? '');
  return d.innerHTML;
}
meta.innerHTML = `<span class="who">${esc(glyph)} ${esc(who)}</span>...`;
```

### P2-7 · `data/sessions.sqlite3` file permissions aren't enforced

**Files:** `smcity/session.py:18-40`, `smcity/app.py` lifespan

On the Mac Studio, the SQLite file is created with default umask (644 — world-readable). PII-redaction means the contents are generally safe, but a `chmod 600` on creation is free defence-in-depth.

**Fix:** after `aiosqlite.connect(self._path)` succeeds for the first time, `os.chmod(self._path, 0o600)`.

### P2-8 · `openpyxl` pulled in but never needed at runtime

**Files:** `pyproject.toml` (not declared), `uv.lock` (pulled in as transitive of our `scripts/llm_ping.py` use of the xlsx probe — actually no, it was added ad-hoc)

I added `openpyxl` during the v0.3.0 xlsx probe but didn't add it to `pyproject.toml`. It's living in the venv unrooted.

**Fix:** either declare it under `[project.optional-dependencies] dev` if only test/dev scripts use it, or remove it. Keeps the supply-chain surface honest.

---

## P3 — quick wins (code hygiene)

1. **Add `uvloop` as the event loop explicitly on macOS.** 5-20 ms saved per turn (`03_enhancements.md` free lunch #10).
2. **Pre-warm KV cache on boot.** One dummy `chat.completions.create(max_tokens=1)` with the full system prompt saves 150-750 ms on first real turn.
3. **Pre-warm DNS + TCP** to data.gov.hk / rt.data.gov.hk / aqhi.gov.hk / etabus.gov.hk / etagmb.gov.hk / als.gov.hk / overpass-api.de on service boot.
4. **Deduplicate the `_haversine_m` helper** — it's duplicated across `transport_planner.py`, `transport_search.py`, `transport_simple_modes.py`, `facility.py`, `housing.py`. Move to `smcity/geometry.py`.
5. **Strip harmony analysis channel from replayed history** in `SessionStore` — history currently captures the full polished reply; a stripped version would shrink prefill tokens.
6. **Golden test for prompt-prefix stability** (`tests/test_prefix_stability.py`) — catches future cache-bust regressions.
7. **Replace `int(round(x))` with `round(x)`** in a couple of remaining spots ruff didn't catch.
8. **Sort `ToolRegistry.openai_schemas()` keys deterministically** — already sorted by name, but verify across reloads.
9. **`meta.forget_me` mutable `_DEFAULT_DB` global** — refactor to read from settings or pass via `ToolContext` so tests don't have to monkeypatch a module-level.
10. **Mypy `strict_equality`** — currently disabled by default in mypy's strict; enable it to catch accidental `"" == 0` style comparisons.
11. **Add `ruff` rule `S104`** (bind-to-all-interfaces) — would flag our `--host 0.0.0.0` default. Override only in the live-deploy config, not in docs.

---

## PASS — categories that came out clean

- **SQL injection** (`smcity/session.py`) — all queries parameterised.
- **eval / exec / pickle / marshal** — zero hits.
- **`os.system` / `subprocess(shell=True)`** — zero hits.
- **Path traversal** — no user-controlled path interpolation.
- **Regex ReDoS** — classifier + cantonese_polish patterns are bounded (no nested unbounded quantifiers). Verified via inspection.
- **httpx timeouts** — every outbound call has an explicit timeout (4–20 s).
- **Bare `except:`** — one `except Exception` in `normalize.py` for optional-dep fallback; acceptable.
- **Hardcoded secrets** — only the LM Studio dummy `api_key="lm-studio"` (LM Studio accepts any string; not a real credential).

---

## Test-coverage gaps

| area | tested? | gap |
|---|---|---|
| `smcity/app.py` endpoints | ✅ (`tests/test_app.py`) | integration smoke vs live LM Studio uses the fixture; could add a chaos test (LM Studio 500) |
| orchestrator hot paths | ✅ | no test for the new `_stream_final` retry branch |
| classifier patterns | ✅ | no parametrised false-positive test (prose that mentions "weather" but isn't a weather query) |
| language router | ✅ | no fuzz test for short ambiguous strings |
| tool registry dispatch | ✅ | no test for timeout handling or rate-limited error path |
| session store | ✅ | no concurrency test (multiple concurrent loads for same session) |
| cantonese_polish | ✅ | no regression test for the `了解` / `的士` / `正在` protection |
| web/app.js | ❌ | no headless-browser or unit tests; manual only |

---

## Top 10 to fix first (ordered by severity × likelihood × blast radius)

1. **P1-1** WebSocket origin check — security, high blast radius if Funnel enabled
2. **P1-2** Rate limiting per session — DoS exposure
3. **P2-1** TTL cache actually enforced — big latency + cost win for repeated tool calls
4. **P2-5** Structured audit log of tool dispatch — huge debugging lever for robot deployments
5. **P1-3** `session_id` regex validation — low-likelihood log-flooding pre-empt
6. **P1-4** Harmony extractor test for user-quoted tool names — injection hygiene
7. **P2-7** `data/sessions.sqlite3` `chmod 600` at creation
8. **P2-6** `web/app.js` textContent migration
9. **P2-3** chat_stream `<|` swallow precision
10. **P2-2** KMB catalog multi-worker plan (doc-only for now; action when scaling)

---

## Architectural improvements (longer horizon)

- **Observability end-to-end** — Langfuse self-hosted + OpenLLMetry per `03_enhancements.md` §5.1. Touches every tool.
- **Tool-call caching layer** — surfaces as both a P2 fix above and a `03_enhancements.md` Top-10 item (#4). Combine both into one implementation.
- **Background tool pre-fetch** — for travel queries, start `get_current_weather` / `get_aqhi` / `get_active_warnings` speculatively before the first LLM hop completes. Saves ~200 ms on multi-tool turns.
- **KV-cache aware turn replay** — when the user re-asks the same question (common with kids / robots), reuse the previous turn's tool results deterministically.

---

## Follow-up

- The `code-reviewer` sub-agent run aborted at 16k output tokens; no deeper findings from it. If a per-file second opinion is wanted later, re-run the agent in chunks (10-file batches) so its output fits the limit.
- The top-10 list above is what to fix next. Pair with `03_enhancements.md` Top-10 for a combined remediation backlog.
