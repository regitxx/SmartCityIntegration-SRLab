# Architecture — HK Smart City Agent

**Version:** 2026-04-21 · v0.1 (pre-implementation)
**Owner:** Lab of Social Robotics
**Status:** Design — to be validated with first spike before code is committed.

---

## 1. System goals → architecture principles

| Goal (from `docs/GOAL.md`) | Architectural consequence |
|---|---|
| Conversational latency (p50 ≤ 1.5 s) | One LLM hop per turn; parallel tool calls; KV-cache warm; avoid planner LLM on the hot path. |
| Cantonese first, multilingual second | Dedicated language router + Cantonese post-processor, separate from the reasoning LLM. |
| Every external read is a tool call | Strict tool registry with pydantic schemas; no free-form HTTP from the LLM. |
| Session memory | In-process slot state + SQLite WAL checkpoint per session, PII-redacted at ingress. |
| Integration-ready for a future robotics platform | Transport-agnostic agent service (WebSocket / SSE / REST); hardware layer never touches LLM internals. |
| Secure, clean, small | ~1–1.5k LOC core; secrets via env; Tailscale-only exposure in dev; pydantic validation at every seam. |

---

## 2. Block diagram

```
                 ┌──────────────────────────────────────────────────────────────┐
                 │                  ROBOT / CHAT UI (future)                    │
                 │    mic → STT → text ; text → TTS → speaker (not in v0)        │
                 └───────────────┬───────────────┬──────────────────────────────┘
                                 │               │
                         WebSocket / SSE    POST /turn
                                 │               │
                 ┌───────────────▼───────────────▼──────────────────────────────┐
                 │                 AGENT SERVICE (FastAPI, async)              │
                 │                                                              │
                 │   1. Ingress:                                                │
                 │      - Auth (Tailscale-only; API key later)                  │
                 │      - Rate limit per session                                │
                 │      - PII scrub                                             │
                 │                                                              │
                 │   2. Language Router                                         │
                 │      - Particle heuristic (嘅/喺/咗/冇/佢/㗎/喎/…) @ 0.92   │
                 │      - fastText lid.176 (non-Chinese fast path, <1 ms)       │
                 │      - HIT-TMG/LID-HK XLM-RoBERTa (primary, 25–60 ms)        │
                 │      - CLD3 (简/繁 disambiguation)                           │
                 │      → { primary_lang, script, code_switch, tts_locale }     │
                 │                                                              │
                 │   3. Script Normaliser (OpenCC s2hk, hk2s on egress)         │
                 │                                                              │
                 │   4. Slot-Filling State Machine                              │
                 │      - Slots: origin, destination, mode, time,               │
                 │        accessibility, venue_type, horizon, session_locale    │
                 │      - clarify_vs_guess(entropy × cost_of_wrong)             │
                 │                                                              │
                 │   5. LLM Orchestrator (OpenAI SDK → LM Studio)               │
                 │      - System prompt PINNED for KV-cache reuse               │
                 │      - Deterministic tool-schema ordering                    │
                 │      - parallel_tool_calls=True                              │
                 │      - Bounded ReAct (max 2 reflective steps)                │
                 │      - Streaming tokens → TTS chunking downstream            │
                 │                                                              │
                 │   6. Tool Dispatcher                                         │
                 │      - pydantic-validated arguments                          │
                 │      - asyncio.gather for independent calls                  │
                 │      - Per-tool circuit breaker + TTL cache                  │
                 │      - Entity-linker post-check (no hallucinated stop IDs)   │
                 │                                                              │
                 │   7. Response Formatter                                      │
                 │      - Cantonese post-process (YueLLM-7B or Qwen2.5-HK)      │
                 │        when user wrote Cantonese and LLM replied in formal 繁體 │
                 │      - Source + timestamp footer                             │
                 │                                                              │
                 │   8. Session Store (SQLite WAL, msgspec, PII-redacted)       │
                 │                                                              │
                 │   9. Telemetry: OpenLLMetry → Langfuse (self-hosted)         │
                 └──────────────┬──────────────┬──────────────┬─────────────────┘
                                │              │              │
         ┌──────────────────────▼───┐  ┌──────▼──────┐  ┌────▼────────────────────┐
         │  LM Studio (Mac Studio)  │  │  Local data │  │  HK gov open-data APIs  │
         │  openai/gpt-oss-120b     │  │  indexes:   │  │                         │
         │  Tailscale:1234/v1       │  │  - GTFS     │  │  MTR · KMB · Citybus ·  │
         │  OpenAI-compatible       │  │  - stop     │  │  NLB · GMB · Ferry ·    │
         │                          │  │    aliases  │  │  HKO · EPD · TDAS ·     │
         │  Optional draft model:   │  │  - LCSD     │  │  TD CCTV · LCSD · HKHA  │
         │  gpt-oss-20b (specul.)   │  │    venues   │  │  · RVD · ALS · CSDI     │
         │                          │  │  - OTP2     │  │                         │
         └──────────────────────────┘  └─────────────┘  └─────────────────────────┘
```

---

## 3. Component contracts

### 3.1 Agent service — public API

- `POST /turn` (JSON, one-shot): `{ session_id, text, locale?, user_location?, tts_locale? }` → `{ text, citations[], lang, followups[], tool_trace[] }`.
- `GET /ws/:session_id` (WebSocket, streaming): bidirectional with incremental `tool_call.start`, `tool_call.result`, `token`, `final` events.
- `GET /sse/:session_id` (SSE, read-only stream): same event stream, server-push only.
- `GET /health`, `GET /metrics` (Prometheus).

**Why three transports:** WebSocket for the robotics SDK (full duplex, clean interrupt), SSE for a browser chat UI (firewall-friendly), REST `POST /turn` for batch eval + CI tests.

### 3.2 Tool registry

- Every tool is a pydantic `Tool` class with:
  - `name` (stable, `snake_case`, prefixed by domain: `transport.`, `context.`, `facility.`, `housing.`).
  - `description` (EN) — must mention what languages the upstream source supports.
  - `args_schema` (pydantic `BaseModel`).
  - `result_schema` (pydantic `BaseModel`).
  - `handler: async def handler(args, ctx) -> Result`.
  - `ttl_seconds` — per-tool cache TTL.
  - `budget_ms` — soft timeout used by the dispatcher.
  - `upstream_langs` — `{"en","tc","sc",…}`.

Tool list source-of-truth lives in `docs/architecture/TOOL_CATALOG.md`.

### 3.3 Language router — contract

Input: raw user text + optional carried locale.
Output:
```python
class LangDetection(BaseModel):
    primary_lang: Literal["yue","zho","eng","jpn","kor","fra","deu","spa","tha","fil","ind","vie","...other"]
    script: Literal["Hant","Hans","Latin","Mixed","Other"]
    is_code_switched: bool
    code_switch_langs: list[str]
    confidence: float  # 0..1
    method: Literal["particle","fasttext","transformer","ensemble","carried"]
    tts_locale: str    # e.g. "yue-HK", "zh-HK", "zh-CN", "en-US"
```

Rules:
1. Particle heuristic → if fires, return `yue` with `confidence=0.92`.
2. fastText → if non-Chinese and confidence > 0.9, return.
3. HIT-TMG/LID-HK transformer → authoritative decision for anything remaining.
4. CLD3 → only invoked when transformer returns `zho` and script field is ambiguous.
5. If all gates uncertain → ask the user in EN + 繁體 which language they prefer and persist on the session.

### 3.4 Slot-filling state machine — contract

`SessionSlots` pydantic model persisted per `session_id`:
```python
class SessionSlots(BaseModel):
    origin: Optional[LocationSlot]
    destination: Optional[LocationSlot]
    mode: Optional[Literal["mtr","bus","minibus","tram","ferry","taxi","walk","drive","any"]]
    depart_time: Optional[AwareDatetime]   # tz=Asia/Hong_Kong
    accessibility: Optional[list[str]]      # "wheelchair", "no_stairs", "avoid_outdoor"
    venue_type: Optional[str]               # "basketball_court", "library", "pool", "park", …
    horizon: Optional[timedelta]            # e.g. "next hour"
    locale: Locale                          # primary_lang + script + tts_locale
    meta: dict                              # timestamps, last_tool, entropy estimates
```

Policy: `clarify` if filling the missing slot via a tool call would cost more than asking, weighted by cost-of-wrong. Encoded as a table in code, not a prompt, so the behaviour is testable.

`ask_user` is itself a tool — it writes to the session slot and emits a short clarification question via the response formatter.

### 3.5 Session store

- SQLite with WAL mode; one row per `session_id`.
- Serialise state with `msgspec` for speed.
- At ingress: a small PII scrubber strips obvious phone numbers / ID card patterns from the text before storing.
- TTL: 24 h default, 0 for "private mode".
- "Forget me" deletes the row and purges the Langfuse trace.

### 3.6 LLM orchestrator loop

```
on_turn(session, text):
    lang = language_router(text, session.slots.locale)
    text_canon = normalise(text, lang)
    slots = update_slots(session, lang, text_canon)
    plan = decide_action(slots)               # "clarify" | "call" | "answer"

    if plan == "clarify":
        return formatter.clarify(slots, lang)

    messages = build_messages(system_prompt, session.history, text_canon, slots)
    resp = llm.chat(messages, tools=registered_tools, parallel_tool_calls=True, stream=True)

    if resp.tool_calls:
        results = await asyncio.gather(*[dispatch(tc) for tc in resp.tool_calls])
        messages += tool_result_messages(results)
        resp = llm.chat(messages, tools=registered_tools, stream=True)  # 2nd hop

    # bounded ReAct fallback
    if resp.tool_calls and react_steps < 2:
        ...

    return formatter.finalise(resp, lang, sources=results)
```

### 3.7 Response formatter

- If `lang.primary_lang == "yue"` and the LLM reply is formal 繁體, pass through a Cantonese post-processor (YueLLM-7B or Qwen2.5-7B-Instruct prompted with Cantonese style exemplars) — this is the mitigation for gpt-oss-120b's weak written-Cantonese surface form documented in `04_multilingual_language_stack.md`.
- Always append a compact "source · timestamp" footer for auditability.
- Emit a structured `language_coverage` block so the UI can show a chip like "answered in Cantonese · MTR API supports EN/繁體".

---

## 4. Deployment topology (dev → prod-ish)

- **Mac Studio (tailscale `earnests-mac-studio`, 100.66.65.36):**
  - LM Studio serving `openai/gpt-oss-120b` on :1234.
  - Agent service (FastAPI, uvicorn) on :8080.
  - Langfuse (self-hosted, Docker) on :3000.
  - SQLite file alongside the service.
- **Developer laptops (tailscale):**
  - Consume `https://earnests-mac-studio:8080` via Tailscale Serve (HTTPS inside tailnet).
- **Future robotics platform:** same consumer contract (WebSocket/SSE/REST); no code changes in the agent core.

Tailscale ACLs: agent service accepts only from the tagged `robot` and `dev` nodes. No Funnel (no public exposure) until product owner approves.

---

## 5. Cross-cutting concerns

### Security
- No secrets in repo. `.env` with `direnv`; `settings.py` loads via `pydantic-settings`.
- Tool handlers validate all arguments; no string interpolation into URLs.
- Rate-limit per session at ingress.
- Audit log: every `tool_call` → `(session, tool, args_hash, result_hash, ts)` into SQLite.

### Observability
- OpenLLMetry shim around the OpenAI SDK.
- Span per turn; child spans per tool call.
- Langfuse for qualitative trace replay; Prometheus `/metrics` for SLOs.

### Reliability
- Per-tool circuit breaker (e.g. `purgatory` or small custom) — opens after N failures, serves last good cached value if TTL allows.
- Bounded retries (exp backoff, max 3) on 5xx; no retry on 4xx.
- Graceful degradation: if `get_mtr_service_status` fails, still answer with `get_mtr_next_trains` but flag the disruption field as "unknown".

### Performance
- `parallel_tool_calls=True` for independent fetches.
- `stream=True` plus incremental TTS chunking in the downstream robotics layer.
- Pin the system prompt; sort tools deterministically; pin one `session_id` to one llama.cpp slot.
- Optional: enable speculative decoding with `gpt-oss-20b` as the draft model once base latency is measured.
- Tiny local classifier (linear-model heuristic) short-circuits obvious intents (weather, AQI) without hitting the big model.

### Testing
- Golden-set eval: ≥ 20 HK queries in Cantonese + 繁體 + EN, scored on TSR, Slot F1, Tool-Selection Accuracy, RAGAS Faithfulness, latency p50/p95.
- Contract tests per tool against live upstream in a nightly job.
- Offline replay: every audit-logged turn can be re-run against pinned tool results.

---

## 6. Technology choices — pros / cons

**Runtime language: Python 3.12**
- Pro: the LLM/ML ecosystem lives here; async HTTP via `httpx`; pydantic; LM Studio examples.
- Con: the JVM of OpenTripPlanner will be a separate process — fine.

**Web framework: FastAPI + uvicorn**
- Pro: first-class async, WebSocket & SSE, pydantic-native, lowest ceremony.
- Con: none meaningful for this use case.

**Orchestration: custom lightweight orchestrator**
- Pro: lowest latency, zero lock-in, clean seams.
- Con: we write the state machine ourselves. Mitigated by keeping it ~400 LOC and heavily typed.
- Alternative considered: **LangGraph** (keep on the shelf if we need persistent checkpointers + graph visualisation).

**LLM: `openai/gpt-oss-120b` via LM Studio**
- Pro: already running on the Mac Studio; OpenAI-compatible; native tool calls via harmony.
- Con: Cantonese surface-form quality; mitigated by a Cantonese post-processor stage.

**Cantonese post-processor: Qwen2.5-7B-Instruct (or YueLLM-7B if licensing clean)**
- Pro: cheap, fast, much better Cantonese register.
- Con: second model in memory. Load on demand or share VRAM budget.

**Language detection: HIT-TMG/LID-HK + fastText + particle heuristic**
- Pro: best HK-specific accuracy in the literature.
- Con: two model downloads. Acceptable.

**Script normalisation: OpenCC (`s2hk` / `hk2s`)**
- Pro: standard; HK-specific config preserves Cantonese characters.
- Con: need to bypass when Cantonese particles detected so we don't mangle 嘅→的.

**Routing engine: OpenTripPlanner 2**
- Pro: battle-tested GTFS + GTFS-RT multimodal router; HK deployments exist in the community.
- Con: JVM footprint. Ran as sidecar. Alternative Valhalla for walking/driving.

**Session store: SQLite WAL + msgspec**
- Pro: zero-ops, fast, safe enough for single-node.
- Con: clustering is a non-goal for v0.

**Observability: OpenLLMetry → Langfuse (self-hosted)**
- Pro: traces LLM + tool calls uniformly; OSS; runs on Mac Studio.
- Con: one more container.

**TTS locale passthrough (future): Azure `zh-HK-WanLungNeural` primary, Fish Speech self-hosted fallback**
- Pro: Azure is the gold standard for Cantonese prosody today; Fish Speech for private/offline.
- Con: cloud fee on Azure — confirm budget with the user.

---

## 7. What this architecture explicitly does NOT do

- Anything that requires persistent per-user accounts — v0 is session-scoped.
- Door-to-door routing that treats indoor MTR interchanges as first-class — we surface station-level detail only.
- "Agentic web browsing" — all reads go through the tool registry.
- Real-time red-minibus ETAs (no feed exists).
- Legal/eligibility advice about housing applications (see `02_*.md` §1.3).

---

## 8. References

- `docs/research/01_datagovhk_transport_apis.md`
- `docs/research/02_datagovhk_housing_context_apis.md`
- `docs/research/03_agentic_tool_calling_architecture.md`
- `docs/research/04_multilingual_language_stack.md`
- `docs/research/05_literature_and_prior_art.md`
