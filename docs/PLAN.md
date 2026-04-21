# Implementation Plan — HK Smart City Agent

**Project:** Smart City Integration · HK Lab of Social Robotics
**Version:** 2026-04-21 · v0.1
**Related:** `docs/GOAL.md` · `docs/architecture/ARCHITECTURE.md` · `docs/architecture/TOOL_CATALOG.md` · `docs/research/0{1–5}_*.md`

This plan breaks the system into phased, shippable spikes. Each phase has a demo target the user can actually run, a clean acceptance criterion, and explicit "what I need from you" blockers.

---

## Guiding principles (non-negotiable)

1. **Ship thin vertical slices.** Each phase must demo end-to-end: mouth (chat in) → brain (LLM + tools) → mouth (chat out). No building pipes without a demo at the end.
2. **Cantonese is tested, not assumed.** Every phase adds a Cantonese test case to the golden set before it's declared done.
3. **Every external read is a tool call.** No phase bypasses the tool registry.
4. **Ruthlessly measure latency.** p50 and p95 latency are tracked from Phase 1 onwards. If a phase regresses beyond budget, we stop and fix before adding more.
5. **Keep the robot seam clean.** The agent service exposes WebSocket / SSE / REST — never embed robot-specific code into the core.

---

## Phase 0 — Foundations (1–2 days)

**Goal:** Empty repo → runnable scaffold with CI, settings, and a "hello world" that talks to LM Studio.

**Tasks:**
- `pyproject.toml` with `uv` (or `hatch`), Python 3.12, strict mypy / ruff configs.
- `smcity/` package layout: `smcity/{service,router,slots,orchestrator,tools,session,langrouter,formatter}`.
- `settings.py` (pydantic-settings) with `LLM_BASE_URL`, `LLM_MODEL`, `LLM_TIMEOUT_S`, `ENABLE_TAILSCALE_ONLY`.
- FastAPI app with `/health` and `/turn` (one-shot, no streaming yet).
- One integration test that hits LM Studio and asserts `openai/gpt-oss-120b` returns a tool-call for a hard-coded weather query.
- Pre-commit: ruff + mypy + `pytest -q` smoke.
- GitHub Actions (or local `just check`) mirroring the above.
- Scripts: `just llm-ping`, `just serve`, `just eval`, `just replay <session>`.

**Acceptance:** `just llm-ping` returns `openai/gpt-oss-120b` ready; `curl POST /turn '{"session_id":"t","text":"ping"}'` returns a plain reply.

**What I need from you before starting:** git remote URL (Q1) so the scaffold lands on main from day 1.

---

## Phase 1 — MVP Transport Agent, EN + 繁體 (≈ 5 days)

**Goal:** Answer the hero scenario "I'm in Sheung Wan, how do I get to Sha Tin?" end-to-end, in English and Traditional Chinese, with real data.gov.hk calls.

**Components:**
- Language router v1: particle-heuristic + fastText, no transformer yet. 繁體 treated as 繁體; 简体 converted to 繁體 HK via OpenCC `s2hk`.
- Session store (SQLite WAL + msgspec) with the `SessionSlots` model from the architecture doc.
- Slot-filling state machine with `clarify_vs_guess` logic.
- Tool registry with the 13 v0 tools listed in `TOOL_CATALOG.md`.
- Tools hot for this phase:
  - `geo.address_lookup` (ALS)
  - `transport.find_stops_near_point`, `transport.find_stops_by_name`, `transport.alias_stop`
  - `transport.get_mtr_next_trains`, `transport.get_kmb_eta_by_stop`, `transport.get_citybus_eta_by_stop`
  - `transport.plan_multimodal_route` — backed by a locally deployed **OpenTripPlanner 2** with today's HK GTFS. (`otp.jar` as a sidecar process, not in-process.)
  - `meta.ask_user`
- FastAPI `/ws/:session_id` WebSocket emitting `tool_call.*` + `token` + `final` events.
- Minimal browser chat UI: a single static page (HTMX or vanilla JS) that connects to `/ws/:session_id`, displays the tool trace in a sidebar, and shows final text in the main pane. Simple but lets the user see exactly what the agent is doing.
- Observability: OpenLLMetry SDK wrapping the OpenAI client; Langfuse optional at this phase (stand up in Phase 2).

**Golden set v0.1:** 20 queries in EN + 繁體, 10 of which are multi-turn ("how do I get from X to Y?" → clarification → final answer). At least 4 must intentionally stress disambiguation.

**Acceptance:**
- `just eval` reports ≥ 80% TSR, ≥ 0.80 Slot F1, median latency ≤ 2.0 s (we target 1.5 s but allow slack until Phase 4 micro-opts).
- The hero scenario works end-to-end in Cantonese-particle and 繁體 inputs (even though the LLM may reply in formal 繁體 — fine for this phase).
- No hallucinated stop IDs (enforced by the entity-linker post-check).

**What I need from you before finishing:** confirmation on chat-UI style (Q4); confirmation that location grounding (Q7) is manual in v0 — i.e. the user types origin until the platform pushes GPS.

---

## Phase 2 — Cantonese polish + Context signals (≈ 4 days)

**Goal:** Make Cantonese the first-class input language with natural-register Cantonese output, and fold weather / AQI / warnings into answers.

**Components:**
- Language router v2: add HIT-TMG/LID-HK transformer + CLD3 for script disambiguation. Ensemble rule (particle → fastText → transformer).
- Cantonese post-processor: load Qwen2.5-7B-Instruct (or YueLLM-7B) on the Mac Studio as a second LM Studio slot; invoke only when `primary_lang == "yue"` and the main LLM replied in formal 繁體. Prompted with Cantonese style exemplars. (Pros/cons of running a second model resident: memory trade vs response quality — see `03_*.md`.)
- Jyutping input support via `pycantonese` + `geo.jyutping_to_text`.
- Context tools: `context.get_current_weather`, `context.get_9day_forecast`, `context.get_active_warnings`, `context.get_aqhi`, `context.get_live_service_summary`.
- Orchestrator learns to **fan out** weather + AQI + MTR status in one `parallel_tool_calls` batch for travel queries — measured latency save target: 200–400 ms per turn.
- Langfuse self-hosted online. Dashboards for latency p50/p95, tool-selection accuracy, Cantonese detection rate.

**Golden set v0.2:** add 15 Cantonese-first queries (colloquial Cantonese, code-switched Canto-English, Jyutping one-liners).

**Acceptance:**
- Cantonese detection ≥ 95% on the golden set.
- Answers in natural Cantonese when the user wrote Cantonese. A native HK reviewer rates ≥ 4/5 on tone/register (informal acceptance — we're not publishing a paper yet, just sanity check).
- Weather / AQI / warnings show up automatically when relevant (e.g. "outdoor court" triggers AQHI check).
- Median latency ≤ 1.7 s. p95 ≤ 3.2 s.

**What I need from you before finishing:** decision on Azure TTS vs. local Fish Speech for Cantonese (Q10, budget). Affects whether the agent service emits an SSML-ready response or a plain-text response for the downstream TTS.

---

## Phase 3 — Housing + Facilities + vague-intent scenarios (≈ 4 days)

**Goal:** Unlock the "basketball court" and "housing info" scenarios. Handle vague inputs correctly.

**Components:**
- Tools: `facility.find_nearby`, `facility.get_details`, `facility.get_availability`, `facility.get_opening_hours`, `housing.get_estate_info`, `housing.list_estates_in_district`, `housing.get_property_market_stats`, `housing.get_waitlist_aggregate_stats`.
- Geo tools: `geo.reverse_geocode`, `geo.location_search`.
- Disambiguation table tuned against real user queries: "basketball court" alone → ask `{location, mode}` first; "closest basketball court to me" → use stored user location + ask `mode`; "court in Kowloon Bay" → accept, list top 3, ask which.
- Explicit safe-wording template for housing queries: agent never claims to check personal application status; redirects to official eligibility checker with a localised link.

**Golden set v0.3:** add 10 facility + 10 housing queries in all three languages.

**Acceptance:**
- Disambiguation recall ≥ 95% on vague queries.
- Housing queries return correct public info with citations and never wander into speculation.
- Facility queries combine static directory + booking availability coherently.

**What I need from you before finishing:** scope call on housing (Q8) — just lookups, or also eligibility walk-throughs (latter is legal-risk).

---

## Phase 4 — Latency & robustness hardening (≈ 3 days)

**Goal:** Hit the production latency SLO and the graceful-degradation target before any platform hand-off.

**Components:**
- KV-cache pinning: stable system prompt + deterministic tool-schema ordering + `session_id` → single llama.cpp slot. Measure time-to-first-token before/after.
- Turn on `parallel_tool_calls=True` where not already, add incremental partial-JSON parsing for parallel arg dispatch.
- Tiny pre-classifier: a linear-model heuristic (scikit or even hand-coded feature extractor) that short-circuits "weather" / "what time is it?" / "MTR status" without hitting the big model. Target: cut those turns to < 400 ms.
- Speculative decoding (optional): run `gpt-oss-20b` as a draft model for the 120b. Gate on measured improvement > 15%.
- Circuit breakers + bounded retries on every tool.
- Per-tool chaos test (`upstream returns 500 / 429 / timeout`) and verify the degraded reply is still useful.

**Acceptance:**
- p50 ≤ 1.5 s, p95 ≤ 3.0 s on golden set.
- Graceful-degradation rate ≥ 80% under chaos.
- No regression on TSR / Slot F1 / Canton detection.

**What I need from you before starting:** confirm whether to spend cycles on speculative decoding (Q10 budget) — it pays off but requires more VRAM.

---

## Phase 5 — Integration-ready service surface + docs (≈ 3 days)

**Goal:** Hand-off-ready. The future robotics platform can wire in without touching the core.

**Components:**
- Harden WebSocket protocol; document message schema.
- SSE variant (for browser clients); REST `POST /turn` (for CI and batch eval).
- Pluggable transport adapter interface (trait) so a ROS2 bridge, gRPC, or custom WebSocket variant slots in.
- Config hot-reload for the tool registry (operators can register/deregister tools via file without restart).
- Full README + CONTRIBUTING + threat model (what the agent trusts, what it doesn't).
- Tailscale Serve config for HTTPS inside the tailnet; optionally gated Funnel config (off by default).
- Final eval report: the v0.3 golden set metrics, the four-pillar evaluation from `05_*.md`.

**Acceptance:**
- A teammate (or the user) can `git clone`, `just serve`, and hit the agent from another Tailscale node within 5 minutes of a fresh checkout.
- The protocol doc cites every message type the robotics platform will see.

**What I need from you before finishing:** platform specifics (Q1 git remote, Q7 platform location-push expectation).

---

## Phase 6 — Stretch (when lab requests more)

Candidates, in rough order of value:
1. **Ferry / GMB / NLB / Tram** support to close mode coverage.
2. **R5 accessibility isochrones** for "how many courts are within 20 min of me?".
3. **Barrier-free routing** using OTP2 accessibility annotations + LCSD lift data.
4. **Red minibus** via community crawler (explicit "best-effort" label).
5. **Persistent user memory** behind opt-in accounts (only once Q11 security posture is decided).
6. **Audio end-to-end** once the STT upstream and TTS downstream are both wired — probably a short spike.

---

## Cross-phase hygiene

### Pros / cons recap (for the choices that could be revisited)

**Custom orchestrator vs LangGraph.**
- Pro custom: lowest latency, cleanest seams for the robotics transport. Con: we write the state machine.
- Pro LangGraph: checkpointers, state inspection, easy to visualise. Con: extra dependency, slightly more overhead per turn, harder to predict latency.
- **Chosen:** custom for v0, LangGraph held in reserve if disambiguation graph grows complex.

**Cantonese post-processor vs tuning gpt-oss-120b.**
- Pro post-processor: no fine-tuning budget, fast to deploy, easily reversible. Con: two models loaded.
- Pro fine-tuning: single model, theoretically cleaner. Con: cost, data, retraining risk with harmony format.
- **Chosen:** post-processor for v0.

**Self-hosted translation (NLLB-200) vs cloud (DeepL / Google).**
- Pro self-host: private, zero cost, deterministic. Con: NLLB quality for `yue_Hant` is acceptable, not great.
- Pro cloud: higher quality on rare languages. Con: privacy, $$$, vendor lock.
- **Chosen:** NLLB-200 self-hosted by default; cloud translation only behind a feature flag the user explicitly enables.

**Tailscale Serve vs Funnel.**
- Pro Serve (internal only): safer, no public exposure. Con: harder to demo to external stakeholders.
- Pro Funnel (public HTTPS): demo-ready. Con: exposes the agent service to the internet.
- **Chosen:** Serve only for v0; Funnel off unless the user flips it on.

### Risks + mitigations

| Risk | Mitigation |
|---|---|
| gpt-oss-120b Cantonese surface quality worse than expected | Post-processor (Phase 2); eval against HKU Cantonese benchmark; fall-back: Qwen2.5-HK |
| LM Studio parallel_tool_calls silently drops array | Explicit test in Phase 0 + CI; fallback to serial tool fan-out |
| data.gov.hk endpoints change URLs / shapes | Nightly contract tests; per-tool pydantic validation raises loudly |
| Tailscale network hiccups | Local fallback to loopback LM Studio for CI; documented timeout + retry |
| Robot platform's transport ≠ our WebSocket | Pluggable adapter interface (Phase 5); spec early in Phase 5 |
| Scope creep into legal/housing advice | Firm "no personal application status" rule; localised safe-response templates |
| Cantonese eval subjectivity | Golden set scored by a native reviewer; quantitative metrics (TSR, slot F1, latency) are the gate |

### What I definitely need from you to unblock Phase 0/1

See `docs/OPEN_QUESTIONS.md`. The blockers for shipping Phase 0 are: Q1 (git remote), Q5 (Tailscale exposure), Q11 (PII posture). The blockers for Phase 1 acceptance are: Q4 (UI style), Q7 (location-push), Q12 (golden set origin — I can seed it, you approve).
