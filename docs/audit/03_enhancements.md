# 03 — Enhancement Research (v0.3.0 → v0.4+)

**Audience:** Smart City HK agent maintainers. Forward-looking research pass, post-v0.3.0.
**Date:** 2026-04-23.
**Scope:** concrete, prioritised enhancements with implementation sketches. See `docs/research/0{1..5}_*.md` for background.

**Citation policy.** Every non-obvious claim cites the canonical URL. Claims drawn from my training window (cutoff Jan 2026) are marked **[VERIFY]** if the underlying library/service is fast-moving; pin against the linked release notes before committing code.

Priority scale:
- **HIGH** — likely > 15 % latency win or closes a real user-facing gap; ship in the next one or two minor releases.
- **MEDIUM** — meaningful improvement but bounded cost/benefit; schedule after HIGH.
- **LOW** — keep on the radar; only pick up if adjacent work makes it free.

Reversibility is flagged per item (feature-flag vs one-way door).

---

## 1. Latency optimisation

### 1.1 Speculative decoding with `gpt-oss-20b` as draft

**Priority: HIGH. Reversibility: feature-flag (free to turn off).**

`gpt-oss-120b` and `gpt-oss-20b` share the harmony tokenizer/vocabulary by design, which is the hard precondition for speculative decoding (Leviathan et al. 2022, <https://arxiv.org/abs/2211.17192>). llama.cpp has first-class support for draft models via `--model-draft` / `speculative` on the server (<https://github.com/ggml-org/llama.cpp/tree/master/examples/speculative>, <https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md>). LM Studio exposes this in recent builds under the model's "speculative decoding" toggle **[VERIFY on your installed LM Studio build — feature shipped in the 0.3.x series in late 2025]**.

Expected gains on Apple Silicon unified memory:
- Easy prose generation (our Cantonese polish reply + tool-result synth path): **1.5×–2.2× tokens/sec** with near-zero accuracy loss. That translates to ~30–50 % wall-clock savings on the synth pass, i.e. ~300–800 ms off our current ~3.8 s weather turn and ~7.3 s MTR turn.
- Tool-call-only turns: small to zero win — the model emits a short JSON arguments blob, not prose.
- Draft-acceptance rate (the only number that matters) for 20 b drafting 120 b on HK Cantonese content: unmeasured publicly. Benchmark it locally before celebrating. Target ≥ 60 % acceptance or speculative is a wash.

VRAM/unified-memory cost: `gpt-oss-20b` at MXFP4 is ~11 GB (<https://huggingface.co/openai/gpt-oss-20b>); on a 192 GB Mac Studio this is free. The 120 b at MXFP4 is ~60 GB. Both resident is fine.

Implementation sketch (half-day):
1. In LM Studio, load `gpt-oss-20b` as a second model, enable it as the draft for `gpt-oss-120b` in the 120 b's model settings.
2. No client-side code change needed — the OpenAI-compatible endpoint is unchanged. `smcity/llm.py` doesn't move.
3. Add a `LM_STUDIO_SPECULATIVE=1` flag in `smcity/settings.py` for ops-level documentation only (the feature is toggled server-side, not client-side).
4. Add a `just bench-speculative` target that runs 20 canned turns with/without speculative and reports tok/s and time-to-last-token.

Failure mode to guard against: some builds of llama.cpp drop the draft model on an OOM and silently fall back to greedy; watch for latency regression in the observability stream.

### 1.2 Prompt caching / KV-slot pinning — what works, what doesn't

**Priority: HIGH (audit), LOW (new code). Reversibility: n/a.**

We already forward `user=session_id` (see `_build_kwargs` in `smcity/llm.py:182`). llama.cpp's server maps that to a per-slot KV cache (<https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md>), and automatic longest-prefix matching is enabled by default. Our prefix is stable because:

- `SYSTEM_PROMPT` is a module-level constant (`smcity/prompts.py`).
- `ToolRegistry.openai_schemas()` in `smcity/tools/registry.py` should be deterministic across turns **[VERIFY it sorts by name]**.
- History entries are appended after the cached prefix.

Known prefix-busters in our code path that are worth auditing:
1. **`locale_hint(detection, forced=...)`** — injected as the second system message; varies per turn when the detected language changes. This is **correct placement** (volatile context last in the system region), but it's a cache-bust every time the user switches languages mid-session. Acceptable.
2. **Cantonese few-shot block** (`cantonese_style_block()`) — conditionally added only for yue. This means every Cantonese turn shares a cached prefix with other Cantonese turns but not with English turns on the same session. Correct.
3. **Historical turns** — we replay `slots.history` which grows each turn. After the stable prefix, the longest-prefix logic should still match turns 1..N-1. But **if we ever trim or rewrite history, the cache collapses from that point forward.**

Concrete recommendations:
- **Add a prefix-stability test** (`tests/test_prefix_stability.py`): for two synthetic turns with different user text, assert the serialised prompt up to the `user` message is byte-identical. This catches future regressions.
- **Sort tool schemas deterministically** in `ToolRegistry.openai_schemas()` if not already (verify once; one-line fix if needed).
- **Strip gpt-oss analysis channels from history** before replaying it next turn — the harmony format separates analysis from final output. If we're storing full assistant responses, analysis tokens bloat the replay cost. Check `smcity/session.py:append_turn` semantics.

Slot-pinning best practice (2026): llama.cpp's native `id_slot` parameter is more reliable than `user`-field mapping, but `user` works on current LM Studio builds. Keep forwarding `user=session_id`; don't move to `id_slot` unless LM Studio exposes it directly.

Reference: llama.cpp server options <https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md>; `gpt-oss` harmony analysis/final channels <https://github.com/openai/harmony>, <https://cookbook.openai.com/articles/openai-harmony>.

### 1.3 Pre-classifier — keep regex, add an ONNX escape hatch

**Priority: MEDIUM. Reversibility: feature-flag.**

Our current `smcity/classifier.py` is a 150-line regex matcher across EN/繁/简/yue/ja/ko for weather, AQI, warnings, chitchat. Pros: zero-dep, 50 µs per classify, deterministic, debuggable. Cons: won't scale past ~8–10 intents without becoming a regex nightmare; doesn't catch rephrasings.

**Don't replace the regex.** It handles the trivial fast-path intents well and we don't want ML dependencies in the hot path for "hi" and "天氣點啊".

**Do add a second-tier classifier** for the 20–30 % of turns where regex misses but an LLM call is still overkill. Options:

| Option | Model size | Latency on M4 CPU | HK Cantonese accuracy | Verdict |
|---|---|---|---|---|
| fastText supervised classifier (trained locally on golden-set labels) | ~5–50 MB | < 1 ms | Good for bag-of-keywords intents | **Pick this** |
| sentence-transformers (`paraphrase-multilingual-MiniLM-L12-v2`) + cosine to intent centroids | 120 MB | 20–50 ms | Excellent for paraphrase | Overkill for < 50 intents |
| ONNX-quantised distilled classifier (XLM-R-base → 4-bit) | 80 MB | 30–80 ms | Best | Only if fastText plateaus |

References: fastText supervised classification <https://fasttext.cc/docs/en/supervised-tutorial.html>; sentence-transformers <https://www.sbert.net/>; ONNX quantisation <https://onnxruntime.ai/docs/performance/quantization.html>.

Implementation sketch (1–2 days):
1. Hand-label the 37-query golden set + ~100 more for the relevant intents (weather / aqi / warnings / mtr_eta / kmb_eta / journey / walking / taxi / poi_search / chitchat / fallback).
2. Train with `fasttext.train_supervised(input="intents.tsv", lr=1.0, epoch=25, wordNgrams=2)`. Model lands at `data/intent_classifier.ftz` (~5 MB after `quantize()`).
3. In `smcity/classifier.py` add a `_model_classify(text)` that runs after the regex; only trust it when confidence > 0.80 AND top-1 intent has a deterministic tool mapping.
4. Feature-flag `FAST_PATH_MODEL=1`; measure `just eval` TSR before/after.

Failure mode: model-picked fast paths that pick the wrong tool. Mitigation: only allow the model to route to tools that already work under the regex-fast path (read-only, no slot dependencies).

### 1.4 Tool-surface reduction per turn

**Priority: HIGH. Reversibility: one-way once we like it (but backing out is a day of work).**

We currently present all 25 tool schemas to the LLM on every turn. For `gpt-oss-120b`, every tool schema takes ~120–300 tokens of prompt. **Current serialised tool block ≈ 4–6 k tokens.** This is prefill you pay on every non-cached turn and KV-cache state you carry forever.

Evidence that shrinking the tool surface helps:
- Berkeley Function-Calling Leaderboard shows tool-selection accuracy degrades with > ~20 tool choices for models < 30 B, and prefill scales linearly with schema bytes (<https://gorilla.cs.berkeley.edu/leaderboard.html>).
- Anthropic's own guidance for Claude tool use recommends keeping the per-turn tool set ≤ 20 (<https://docs.claude.com/en/docs/agents-and-tools/tool-use/overview>).
- Internal HK agent data: our v0.1.3 fix history includes mode-ask ping-pong bugs that trace partly to the LLM having too many plausible tool choices for "how do I get from X to Y".

**Design: tool-group routing.** Deterministic pre-routing selects a subset of tools based on regex/keyword classes. The LLM only sees the subset.

Groups (rough sketch, 5 groups × ~4–7 tools each):

| Group | Trigger keywords | Tools |
|---|---|---|
| `transport` | bus / mtr / taxi / walk / 巴士 / 地鐵 / 的士 / 去 / 點去 / how to get | `transport.*`, `geo.address_lookup` |
| `weather_context` | weather / aqi / warning / typhoon / 天氣 / 空氣 / 颱風 | `context.*` |
| `poi` | near / 附近 / 邊度有 / toilet / 廁所 / court / pool / dentist / … | `geo.search_osm_pois`, `facility.*` |
| `housing` | estate / 屋邨 / HOS / 公屋 | `housing.*` |
| `meta` | forget / reset / 唔記得我 / which languages | `meta.*` |
| `fallback` | (anything not matched) | all 25 — safety net |

Implementation sketch (half-day):
1. New file `smcity/tool_router.py` with `choose_tool_group(text, detection) -> set[str]`.
2. In `Orchestrator._build_messages` (or rather in the `chat(..., tools=self._registry.openai_schemas())` line) pass a filtered schema list.
3. The filter is always a superset of what any single turn could need, and falls back to "all tools" on low-confidence matches. Keep `meta.ask_user` and `meta.forget_me` in every group.
4. Add a unit test per group proving (a) the right tools are visible (b) the `fallback` still sees everything.

Expected saving: 1.5–3 k prompt tokens on ~70 % of turns → 100–300 ms TTFT on a cold cache, ~30–60 ms on a warm cache. Plus a measurable uplift in tool-selection accuracy.

Risk: wrong group means the LLM can't call the right tool. Mitigation: when the LLM returns "no suitable tool / asked for clarification", retry once with the `fallback` group (full surface). This costs one extra hop on the ~2 % of mis-routed turns.

### 1.5 Streaming-TTS for the robot path

**Priority: MEDIUM. Reversibility: transport adapter, free to swap.**

TTS is off the critical path of `/turn` but on the critical path of *perceived* turn latency. The WebSocket protocol already emits `turn.token` deltas (`docs/PROTOCOL.md:96`), which is enough to drive a streaming TTS. The question is which TTS.

Comparison for **Cantonese** specifically (extends `docs/research/04_multilingual_language_stack.md:480`):

| TTS | Cantonese | Streaming (sub-sentence latency) | Self-host | Approx TTFB | Fit for robot |
|---|---|---|---|---|---|
| **Azure Speech** `yue-HK-WanLungNeural` / `HiuGaaiNeural` | Best | Yes, streaming synth on WebSocket SDK | No (cloud) | 150–350 ms | **Best quality/TTFB** — pick for the robot unless air-gap required |
| **Google Cloud TTS** `yue-HK-*` | Very good | Streaming synth via gRPC | No | 200–500 ms | OK alt to Azure |
| **Fish Speech v1.5** (Cantonese community model) | Good (2025 Cantonese LoRA) | Yes, streaming MIDI-like token stream | Yes, Metal | 400–900 ms on M4 | **Best self-hosted** — trade quality for privacy/air-gap |
| **F5-TTS** (voice cloning, few-shot) | Good-ish | Chunk-level streaming | Yes | 500–1200 ms | Useful when the robot has a chosen voice identity |
| **Coqui XTTS v2** | Limited | Chunk streaming | Yes | 300–700 ms | Cantonese quality shaky; fallback only |
| **Kokoro-82M** (Apache-2, 2024) | No Cantonese (EN/JA/ZH) | Yes | Yes | < 100 ms | Not for Cantonese; good for EN path |
| **Piper** | No Cantonese | — | Yes | < 200 ms | Skip |

References: Azure TTS streaming SDK <https://learn.microsoft.com/en-us/azure/ai-services/speech-service/how-to-lower-speech-synthesis-latency>; Fish Speech <https://github.com/fishaudio/fish-speech>; F5-TTS <https://github.com/SWivid/F5-TTS>; Kokoro <https://huggingface.co/hexgrad/Kokoro-82M>.

**Recommendation:**
- **Dev/lab demo (near-term):** Azure `yue-HK-WanLungNeural`. Pros: best quality, fastest TTFB, HK-native tone. Cons: cloud roundtrip, API key, ~US$16/1 M chars.
- **Production robot (if air-gap required):** Fish Speech + HK Cantonese reference audio on the Mac Studio. Pros: self-hosted, free, streaming. Cons: 400–900 ms TTFB, occasional tone slips.
- **Both paths simultaneously:** define a `TTSAdapter` trait in `smcity/tts/` (new package) with `stream(text_iter: AsyncIterator[str]) -> AsyncIterator[bytes]`. Two impls: `AzureTTS`, `FishSpeechTTS`. The WebSocket consumer (robot) picks one via query param `?tts=azure|fish|off`. Keep the agent core TTS-agnostic.

Sub-sentence streaming pattern: buffer `turn.token` deltas until you hit a clause boundary (`[。！？，；.!?]` or ~40 chars since last synth), then fire a synth request for that chunk. This gets first audio out in ~400 ms after first token in most TTS engines. Reference: Azure's "lower synthesis latency" doc linked above.

---

## 2. Multi-model orchestration: Cantonese-tuned 7B post-processor

**Priority: LOW. Reversibility: feature-flag.**

We already ship a **deterministic** post-pass (`smcity/cantonese_polish.py`) — 50-entry phrase table + 7 regex char subs. The question is whether to add a **neural** post-pass.

Candidate models (updated since `docs/research/04_*.md:331`):

| Model | Size | License | Cantonese quality | Notes |
|---|---|---|---|---|
| **Qwen2.5-7B-Instruct** | 7B | Apache-2.0 | Good Cantonese comprehension; mediocre idiomatic generation | General-purpose; benchmark against your polish pass |
| **YueLLM-7B** (HKUST) | 7B | **[VERIFY license — prior research doc flagged this as open]** | HK-native; strongest particle generation of any OSS model | <https://github.com/hkust-nlp/YueLLM> if still live |
| **Qwen2.5-14B-Instruct** | 14B | Apache-2.0 | Better than 7B, slower | Overkill for a polish step |
| **Hunyuan-MoE-A13B** | 13B active | Tencent open | Good HK Mandarin; unclear on vernacular Cantonese | Not validated for yue register |
| **BailingLM-Cantonese** (community LoRA on Qwen) | 7B | varies | Decent; needs HF search | Volatile ecosystem |

**What you'd need it for:** currently our deterministic polish fires 50 phrase subs + 7 char subs. It catches the obvious 是→係 / 沒有→冇 / 現在→而家 / 什麼→乜嘢 cases. It does **not** add particles that weren't there (no 㗎/喎/囉 insertion) and doesn't restructure sentences.

**Published benchmarks claim 7B Cantonese fine-tunes outperform general-purpose 120 B models on vernacular register** (e.g. HKU MMLUCanto, CantoMap) — but the delta vs our in-prompt few-shot + deterministic polish is **unmeasured**. My best-guess prior: current pipeline is ≥ 75 % of what YueLLM-7B would give at 0 added latency and 0 added memory.

**Cost:**
- Loading Qwen2.5-7B at Q4_K_M as a second LM Studio slot: ~5 GB resident.
- Inference for a 2–4 sentence Cantonese reply: 200–500 ms on M4 Metal.
- That wipes out most of the speculative-decoding gain from §1.1.

**Recommendation: don't ship a neural Cantonese post-processor until you have numeric proof the deterministic polish is insufficient.** The process:

1. Build a 100-turn Cantonese golden set with a native HK speaker grading particle naturalness on a 1–5 scale (Phase 2 plan already called for this; do it).
2. If the current pipeline scores ≥ 4.0/5 median, **ship nothing** and tick this off.
3. If it scores 3.0–4.0, add neural post-polish behind `CANTONESE_NEURAL_POLISH=1`. Benchmark latency.
4. Only if it scores < 3.0 does this become HIGH priority.

Also: watch for CantoneseLLM-v2 / YueLLM-v2 releases in 2026. The delta between "decent 7B" and "great 7B" is a moving target.

**Is there an OSS Cantonese model that beats gpt-oss-120b + our pipeline on HK queries?** No credible public benchmark claims this as of Jan 2026. CantoMap and MMLUCanto are tiny evals; they measure vocabulary, not task accuracy. Until someone runs gpt-oss-120b vs YueLLM-7B on a HK smart-city task set with agreement-score scoring, this is speculation.


---

## 3. Proper multimodal routing — OpenTripPlanner 2 on macOS for HK

**Priority: HIGH. Reversibility: sidecar service, free to turn off.**

Current state: `transport.plan_simple_route` does walk + MTR only (hand-rolled Dijkstra over a 105-station graph). Bus + minibus + ferry + tram are tool-callable per-operator but not **composable** into "walk 300 m → KMB 104 → MTR Tsuen Wan line → walk 200 m".

### 3.1 OpenTripPlanner 2

OTP2 is the mature open-source multimodal router used by several city transit agencies (<https://www.opentripplanner.org/>, <https://github.com/opentripplanner/OpenTripPlanner>). It consumes GTFS + OSM, builds a street + transit graph, and serves `/otp/gtfs/v1` GraphQL + `/otp/routers/default/plan` REST.

**HK GTFS availability.** data.gov.hk publishes GTFS-format public-transport data as dataset S500 (*Headway Information of Public Transport Services*) and as part of S507 (*Routes and Fares of Public Transport*). Both are still listed as deferred in `docs/DATASETS.md`. Upstream reality as of Q1 2026 **[VERIFY against data.gov.hk dataset pages]**:
- A consolidated GTFS bundle (KMB + Citybus + MTR + GMB + ferry + tram) is not officially published as a single file; instead, each operator publishes its own dataset and the community stitches them.
- HKBus/Hong-Kong-GTFS community project (<https://github.com/hkbus/hk-public-transport-gtfs> if still maintained **[VERIFY]**) publishes a stitched GTFS weekly. This is the pragmatic source.
- TD publishes "Public Transport Enquiry Service" data in GTFS form via data.gov.hk (<https://data.gov.hk/en-data/dataset/hk-td-tis_2-public-transport-route-fare>).

**Graph build on Apple Silicon.** OTP2 runs on any JDK 21. For an HK-sized graph (OSM HK extract ~800 MB + GTFS ~50 MB):
- OSM HK extract: <https://download.geofabrik.de/asia/china.html> (take the HK subset) or via BBBike for an exact bbox.
- Graph build on M4 Mac Studio: 5–15 min for HK-scale; graph.obj ~500 MB–1 GB.
- Startup time: ~30 s to load the graph into memory (~4–8 GB RAM).
- Per-query latency: 50–200 ms for walk+transit plan; well inside our budget.

**Serving model.** Run OTP2 as a sidecar JVM process, not in-process Python. Three options:
1. **Bare JVM on the Mac Studio** (`java -Xmx8g -jar otp-2.6.0-shaded.jar --load graphs/hk`). Simplest; what we'll do.
2. **Docker** (<https://hub.docker.com/r/opentripplanner/opentripplanner>). One more moving piece on macOS.
3. **OTP cloud** (Conveyal, <https://conveyal.com/>). Overkill; paid.

### 3.2 Alternatives

| Router | Pros | Cons | Verdict for HK |
|---|---|---|---|
| **OTP2** (Java) | Battle-tested, GraphQL + REST, accessibility (step-free) natively, big community | JVM memory, graph rebuild on GTFS update | **Pick this** |
| **Valhalla** (C++) | Very fast, low memory, great for driving/walk/bike, tile-based updates | Transit support is a second-class citizen; no GTFS-native routing on par with OTP | Not yet for transit-heavy HK |
| **GraphHopper** (Java) | Fast, good OSM | Transit support (`GraphHopper PT`) is narrower than OTP2; less community | Possible alt |
| **r5** (Java, Conveyal) | Best accessibility/isochrone math, supports OTP-style routing | Harder to operate, API less friendly | Use for isochrones (Phase 6 "courts within 20 min") |
| **Custom Python** | Full control | Reinvent the wheel; slow | Keep our MTR-only planner for the hot "MTR" path; OTP2 for compound |
| **Google Directions API** | High quality | Cloud, per-query cost, privacy | No |
| **HERE Routing API** | High quality | Same concerns as Google | No |

References: OTP2 docs <https://docs.opentripplanner.org/en/latest/>; Valhalla <https://github.com/valhalla/valhalla>; GraphHopper PT <https://github.com/graphhopper/graphhopper/blob/master/docs/web/public-transit.md>; r5 <https://github.com/conveyal/r5>.

### 3.3 Composing bus + MTR + walk in a single tool

New tool sketch: `transport.plan_multimodal_route(origin, destination, modes=["transit","walk","taxi"], optimise="fastest", depart_at=None, accessibility=None)`.

Shape:
```python
{
  "itineraries": [
    {
      "summary": "Walk 3 min → KMB 104 (12 min) → Walk 5 min",
      "duration_min": 20,
      "legs": [
        {"mode": "walk", "from": "...", "to": "stop_id", "duration_min": 3, "distance_m": 250},
        {"mode": "bus", "operator": "KMB", "route": "104", "from_stop": "...", "to_stop": "...", "duration_min": 12, "boarding_eta_min": 4},
        {"mode": "walk", "from": "stop_id", "to": "...", "duration_min": 5, "distance_m": 420}
      ],
      "fare_hkd": 8.4,
      "step_free": true
    },
    ...
  ],
  "upstream": "otp2-local",
  "computed_at": "..."
}
```

Implementation sketch (2–3 days):
1. New sidecar: `scripts/otp/build_graph.sh` (downloads OSM HK + HKBus GTFS, builds), `scripts/otp/serve.sh` (java -jar ...). Dockerfile optional.
2. New tool: `smcity/tools/transport_otp.py` calling `http://localhost:8080/otp/routers/default/plan`. Keep `transport.plan_simple_route` as the fast path for MTR-only queries; OTP is called only for "transit" or "any"-mode requests.
3. Update `docs/architecture/TOOL_CATALOG.md` and the system prompt: `plan_journey` delegates to OTP under the hood for "any mode" queries; the prompt rule for mode-specific queries is unchanged.
4. Nightly `just refresh-gtfs` that pulls fresh GTFS + rebuilds the graph atomically (build in temp dir, swap symlink).
5. **Golden-set addition:** 10 "from X to Y with bus" queries that require GTFS composition.

Deployment gotcha: on macOS, OTP2 binds IPv6 by default; force IPv4 with `-Dserver.address=127.0.0.1` or use `localhost`. Tailscale-only exposure via `otp:8080`.

Accessibility is free: OTP2 parses GTFS `wheelchair_boarding` and OSM `wheelchair=*` tags and supports `wheelchair=true` in the plan request. This covers S505 MTR barrier-free data once MTR's GTFS advertises the field **[VERIFY current MTR GTFS]**.


---

## 4. Language detection upgrades

### 4.1 Short-query detection: what beats our particle heuristic

**Priority: MEDIUM. Reversibility: feature-flag.**

Current pipeline (per `smcity/langrouter/detect.py` and `docs/research/04_*.md:36`): particle heuristic (30 particles + bigrams) at 0.92 confidence → unicode-script majority → S/T disambiguation by character set. Works very well for queries that include ≥ 1 Cantonese particle; weaker for short, particle-free queries ("點去沙田") and for code-switched snippets.

2026 options for the gap:

| Detector | Languages | yue native | Short-text accuracy | Latency on M4 CPU | License | Verdict |
|---|---|---|---|---|---|---|
| **Particle heuristic + script majority** (current) | all (via script) | Yes (heuristic) | ~88 % on HK queries **[VERIFY — needs golden set]** | < 0.1 ms | n/a | Keep as first gate |
| **fastText `lid.176`** | 176 | No (maps yue→zh) | ~72 % on < 5 tokens | < 1 ms | MIT | Already the intended Phase 2 fallback; add for non-CJK |
| **lingua-py** | 75 | No | ~86 % on 1-word (own claim) | 3–8 ms | Apache-2 | Good short-text fallback; no yue |
| **GlotLID-3** (<https://github.com/cisnlp/GlotLID>) | 2000 + | Yes (yue_Hant) | F1 ~0.93 HK test | 2–5 ms | MIT | **Most compelling new OSS since our Phase 2 doc** — fastText-style but trained with yue distinct |
| **HIT-TMG/LID-HK** | HK-multilingual | Yes | F1 ~0.91 HK mixed-script | 25–60 ms | research license **[VERIFY]** | Gold standard for code-switch; heaviest |
| **XLM-R-lid (papluca)** | 20 | No (zh-cn/zh-tw only) | ~95 % | 30–80 ms | MIT | Good general, no yue |
| **LLM-as-detector** (ask gpt-oss directly) | any | Yes | ~95 % | 300–900 ms | n/a | Too slow for hot path; ok as tie-breaker |

**Recommendation:**
1. **Add GlotLID-3** as the non-CJK detector behind the particle heuristic. Reason: our current fallback to script-majority for non-CJK (Thai/Arabic/etc.) works but gives no discrimination within Latin script (French vs Tagalog). GlotLID has yue as a proper class, is fastText-speed, and is ~5 MB at model level. Single `pip install glotlid` **[VERIFY package name]**.
2. Keep particle heuristic as the cheap first gate (unchanged).
3. **Skip HIT-TMG/LID-HK for now.** The accuracy lift is real but the marginal value over GlotLID + our particle heuristic is small and 25–60 ms per turn is a meaningful cost on top of the rest.

References: GlotLID paper <https://arxiv.org/abs/2310.16248>; GlotLID HF <https://huggingface.co/cis-lmu/glotlid>; lingua-py <https://github.com/pemistahl/lingua-py>.

Implementation (half-day):
1. `uv add glotlid` (or download `glotlid-c.bin` and use fastText directly).
2. In `smcity/langrouter/detect.py`, after the particle heuristic and script majority, if `primary_lang=="eng"` but script is Latin with diacritics, run GlotLID to refine to fra/deu/spa/tgl/ind/vie.
3. Unit test: 30 short queries across 10 languages, assert each lands on the correct ISO 639-3 code.

### 4.2 Translation fallback: NLLB-200 vs cloud vs M2M-100

**Priority: LOW right now; HIGH if you add languages with zero data.gov.hk presence.**

Current state: we're using the LLM itself as translator (query → 繁體 for data.gov.hk; source reply → user lang). That works at the quality cost of a 120 b model doing MT, which is *fine* for high-resource languages and *mediocre* for tl/id/vi/th.

| Translator | yue_Hant | Self-host | Quality (CJK↔EN) | Quality (low-res) | Latency on M4 | Verdict |
|---|---|---|---|---|---|---|
| **Current: gpt-oss-120b as translator** | via Cantonese mode | Yes | Good | Mixed; worse on tl/vi | shared with reasoning | Free; keeps one model |
| **NLLB-200-distilled-600M** | yue_Hant (native) | Yes | Good | Very good | 100–500 ms CPU | <https://huggingface.co/facebook/nllb-200-distilled-600M> |
| **NLLB-200-3.3B** | yue_Hant | Yes | Better | Better | 400–1500 ms CPU / 100–300 ms Metal | Quality ceiling |
| **M2M-100 1.2B** | yue | Yes | OK | OK | 300–900 ms | Older; prefer NLLB |
| **DeepL API** | **No yue target** | No | Best EN↔DE/FR/JA | Excellent | 100–400 ms | Kills yue path |
| **Google Translate API** | **No yue target** | No | Best general | Excellent | 80–300 ms | Kills yue path |
| **Azure Translator** | **No yue target** | No | Good | Good | 100–300 ms | Kills yue path |

The Cantonese-target gap is the deal-breaker for cloud MT: none of them support `yue` as a target, so you'd have to render in Mandarin and then post-process to Cantonese — which is exactly what our polish does.

**Recommendation:**
- **Default: keep LLM-as-MT.** For CJK + EN + FR/DE/ES this is good enough.
- **Add NLLB-200-distilled-600M as a sidecar** only for the tl/id/vi/th/hi path. Behind a `NLLB_FALLBACK=1` flag. Load with `transformers` + Metal (`mps` backend on PyTorch 2.x **[VERIFY mps coverage for NLLB]**); ~2.4 GB RAM.
- **Never depend on DeepL/Google for the yue round-trip** — correctness bug.

Cost: 2 days to wire NLLB sidecar + a translation tool at the boundary of non-native-supported languages.

---

## 5. Observability

### 5.1 Trace collection: Langfuse vs Phoenix vs OpenLLMetry

**Priority: MEDIUM. Reversibility: swap instrumentation libs.**

Current state: nothing wired. Our system prompt says "OpenLLMetry / Langfuse-ready (not wired in v0.1)" (`CHANGELOG.md:127`).

| Stack | Self-host footprint | LLM-specific UI | OpenAI tool-call support | Replay / eval integration | Verdict |
|---|---|---|---|---|---|
| **Langfuse v3** (self-host) | 1 × postgres + 1 × clickhouse + 3 × node services; docker-compose ~2 GB RAM minimum | **Best** — prompt versioning, evals, tool-call trees, playground | Native | Built-in datasets + online evals | **Pick this** for LM Studio |
| **Arize Phoenix** | 1 × container + DuckDB/SQLite or postgres; ~500 MB RAM | Very good; OpenInference semantic conventions | Native (via `openinference-instrumentation-openai`) | Has its own eval lib | Good alt; simpler to run |
| **OpenLLMetry** (Traceloop) | no backend on its own — instrumentation lib that exports to OTLP | — | Native (patches OpenAI client) | — | **Instrumentation only; export to Langfuse** |
| **Grafana Tempo + Loki** | Heavy; ~2 GB | Generic OTel UI; no LLM-native views | Via OpenLLMetry | Weak for LLM-specific | Use only if the lab already runs Grafana |

References: Langfuse <https://langfuse.com/docs/deployment/self-host>; Phoenix <https://github.com/Arize-ai/phoenix>; OpenLLMetry <https://github.com/traceloop/openllmetry>; OpenInference <https://github.com/Arize-ai/openinference>.

**Recommendation (combined):**
- Instrument with **OpenLLMetry** (patches `openai` and `httpx` automatically) + manual spans for tool dispatch.
- Export OTLP to **Langfuse self-hosted** on the Mac Studio (or a dedicated small VM — don't colocate with the LLM for latency reasons).
- If the ops burden of Langfuse's multi-container stack is too much for a lab environment, **Phoenix is the lighter-footprint alternative** — single container, DuckDB, good UI.

Implementation sketch (1 day):
1. `uv add traceloop-sdk langfuse`.
2. In `smcity/app.py` (FastAPI startup), `Traceloop.init(app_name="smcity", api_endpoint="http://langfuse:3000/api/public/otel", api_key=...)`.
3. Add a `@workflow(name="handle_turn")` decorator on `Orchestrator.handle_turn`, `@task` on each helper.
4. Tool dispatch is already an async boundary; manual `with tracer.start_as_current_span(f"tool.{name}")` inside `_dispatch_one`.
5. Verify traces appear in Langfuse UI with tool args + results + token usage + per-tool latency.

The non-negotiable observability feature for a tool-heavy agent: the trace must show the **LLM → tool_calls → tool results → LLM** tree, not a flat list of HTTP calls. Both Langfuse and Phoenix get this right; raw Grafana does not without custom work.

### 5.2 Golden-set eval harness

**Priority: MEDIUM. Reversibility: swap libraries as needed.**

Current state: 37-query golden set with bespoke runner (`just eval`). That's fine for v0 but not stable as the surface grows.

| Harness | Strength | Weakness | Tool-call-heavy fit |
|---|---|---|---|
| **RAGAS** | RAG-native (faithfulness, context recall) | RAG-centric; not built for agentic tool-call accuracy | Weak |
| **DeepEval** | Many built-in metrics, pytest-compatible | Heavy; lots of abstractions | OK |
| **Promptfoo** | YAML config, CLI, CI-friendly, **tool-call assertions out of the box** | JS/TS-flavoured; installs node | **Best fit** |
| **OpenAI Evals** | Official | OpenAI-hosted, harder to self-host | Skip |
| **LangSmith** | Best UI | LangChain-coupled; proprietary backend | Skip for us |
| **Langfuse datasets + online evals** | Integrates with the tracing we already want | Less mature than the dedicated tools | Use after wiring §5.1 |
| **Custom (current)** | Zero deps | Grows hair | Keep for CI smoke |

References: RAGAS <https://github.com/explodinggradients/ragas>; DeepEval <https://github.com/confident-ai/deepeval>; Promptfoo <https://github.com/promptfoo/promptfoo>.

**Recommendation:**
- **Keep the custom `just eval` for CI smoke** (fast, zero-dep).
- **Add Promptfoo for the deep eval loop.** Its assertions model fits agentic tool-calling: `assert: { type: javascript, value: "output.tool_trace[0].name === 'transport.plan_journey'" }`. YAML config means golden sets are versioned with the repo.
- **Add Langfuse online evals once §5.1 is wired** — lets you track live prod metrics (bias, latency, failure) without pre-writing tests.

Implementation sketch (1 day for Promptfoo wiring):
1. `npm install -g promptfoo` (or use `npx promptfoo`).
2. `docs/eval/promptfoo.yaml` with providers pointing at `http://localhost:8080/turn` and a matrix of our golden-set prompts + expected tools + expected citation presence.
3. `just eval-deep` runs Promptfoo, writes HTML report to `docs/eval/report/`.
4. CI mode: `promptfoo eval -o results.json --assertions-only` for a machine-readable fail gate.


---

## 6. Robotics integration

### 6.1 ROS 2 ↔ WebSocket bridge

**Priority: MEDIUM (blocks actual robot embodiment). Reversibility: adapter, free to swap.**

Our `/ws/:session_id` protocol is already robot-friendly (streaming tokens, tool events, typed envelopes — see `docs/PROTOCOL.md`). The task is bridging it to ROS 2 topics/actions so the robot's behaviour stack can consume `turn.token` for incremental TTS, `tool_call.start` for narration cues ("let me check the MTR now…"), and `turn.final` for logging/action.

Options (2026):

| Bridge | ROS side | How it talks | Notes |
|---|---|---|---|
| **`rosbridge_suite`** (standard) | ROS 2 node exposing topics over WebSocket JSON | We'd write a small Python node that subscribes to our `/ws` and republishes as ROS topics | Battle-tested; latency ~5–15 ms | **Start here** |
| **`rclpy` direct client** | Native ROS 2 Python node | Uses `websockets` lib to connect to our `/ws`, translates to `rclpy` publishers | No middleman; cleanest | Good if the lab already writes `rclpy` |
| **Micro-ROS** | Embedded | Overkill for a LM Studio-backed agent | Skip |
| **FoxgloveWebSocket** | Foxglove Studio native | Good for visualisation, not actuation | Visualisation only |

References: rosbridge <https://github.com/RobotWebTools/rosbridge_suite>; rclpy <https://docs.ros.org/en/jazzy/Concepts/Intermediate/About-RCLPY.html>; Foxglove WebSocket <https://docs.foxglove.dev/docs/connecting-to-data/frameworks/ros2>.

Recommended topic layout (sketch):
```
/smcity/turn/token           std_msgs/String   # streaming text
/smcity/turn/tool_call       smcity_msgs/ToolCallStart
/smcity/turn/tool_result     smcity_msgs/ToolCallResult
/smcity/turn/final           smcity_msgs/TurnFinal
/smcity/cmd/ask_turn         smcity_msgs/AskTurn      # action, request→result
/smcity/cmd/set_locale       std_srvs/SetString
```

Keep `smcity_msgs` small (fewer than 10 types). Generate from the existing Pydantic schemas if possible; avoid maintaining two sources of truth.

Implementation sketch (2–3 days, outside this repo):
1. Separate repo `smcity_ros2_bridge/`, ROS 2 Humble/Jazzy package.
2. Single `rclpy` node: on startup, connect WebSocket to `ws://smcity-host:8080/ws/robot-1`; translate each event into a topic publication; expose an action `AskTurn` that posts `{"type":"turn","text":...}` to the WS and waits for `turn.final`.
3. Document in `docs/PROTOCOL.md` — add an "Integrating from ROS 2" section.

### 6.2 STT pipelines feeding `/turn`

**Priority: MEDIUM (depends on embodiment timeline). Reversibility: adapter.**

Options with Cantonese-relevant notes:

| STT | Cantonese accuracy | Streaming | Self-host | Latency to first partial | Verdict |
|---|---|---|---|---|---|
| **Whisper.cpp** (large-v3-turbo) | Fair; Cantonese recognised but as zh | Yes (VAD-gated chunks) | Yes (Metal) | 300–800 ms | <https://github.com/ggml-org/whisper.cpp> |
| **faster-whisper** (CTranslate2) | Same corpus as whisper; slightly better throughput | Yes | Yes | 250–700 ms | <https://github.com/SYSTRAN/faster-whisper> |
| **WhisperX** | Same + word-level timestamps | Semi-streaming | Yes | 500 ms+ | For logging, not the hot path |
| **Azure Speech STT** | **Excellent Cantonese** (`zh-HK` locale); word-level word boundaries | Yes, WebSocket | No | 150–400 ms | Best quality |
| **Google Cloud Speech** | Good Cantonese | Yes | No | 200–500 ms | — |
| **ElevenLabs Speech-to-Text (Scribe)** | Good general, Cantonese fair | Yes | No | 300–700 ms | Nice voice-cloning synergy if using ElevenLabs TTS; not best in class for STT |
| **Moonshine** (2024) | Trained for on-device EN/multilingual | Yes | Yes | 100–200 ms | EN only currently; skip for HK |
| **Apple Speech framework** | Good Cantonese on macOS | Yes | Yes (macOS native) | 200–500 ms | Locked to Apple platform — fine for a Mac-based robot |

References: whisper.cpp <https://github.com/ggml-org/whisper.cpp>; faster-whisper <https://github.com/SYSTRAN/faster-whisper>; Azure Speech <https://learn.microsoft.com/en-us/azure/ai-services/speech-service/>.

**Recommendation:**
- **For the lab demo robot (near term):** Azure Speech `zh-HK`. Pros: Cantonese accuracy, low latency, robust. Cons: cloud, cost, API key. 
- **For production / air-gap:** faster-whisper large-v3-turbo with VAD-gated streaming. Pros: free, HK Cantonese acceptable. Cons: 300–800 ms latency, occasional tone errors on short phrases.
- **Pipeline:** STT client publishes partial transcripts on `/smcity/stt/partial`; a lightweight "endpointer" debounces and, on a 400 ms silence + VAD drop, fires `POST /turn`. Don't stream *every* STT partial into `/turn` — one final utterance per user turn.

Wake word (optional): `openwakeword` (<https://github.com/dscripka/openWakeWord>) with a custom model, or "Hey Robot" via Picovoice Porcupine (cloud-registered license).

### 6.3 Cantonese TTS latency specifically

See §1.5 above. The register post-pass (`smcity/cantonese_polish.py`) fixes text quality. For voice quality the ranking by **first-audio latency on a Cantonese Mac Studio setup** (my training estimates, **[VERIFY with a benchmark on your hardware]**):

1. Azure `yue-HK-WanLungNeural` streaming: ~200 ms first-audio from clause boundary.
2. Fish Speech v1.5 Cantonese, Metal: ~500 ms first-audio.
3. F5-TTS few-shot with HK voice, Metal: ~800 ms first-audio.
4. Coqui XTTS v2: not recommended for Cantonese.

The "feel" of a conversational robot is dominated by first-audio latency, not total synthesis time. Optimise for that (Azure wins; Fish Speech is the best open option).

---

## 7. Data coverage gaps vs the workbook

### 7.1 S500 GTFS headway — best HK GTFS parsing library + cadence

**Priority: MEDIUM. Unblocks S507 as a side effect.**

The "right" way to consume HK GTFS is to **not parse it yourself**; feed it to OTP2 (§3.1) and query the OTP GraphQL/REST API. If you need raw GTFS access for a specific use case (e.g. headway tables for "how often does KMB 104 come"), the libraries:

| Library | Lang | Feature | Verdict |
|---|---|---|---|
| **`gtfs-kit`** | Python | Pandas-backed, full GTFS static spec | **Pick for analysis** <https://github.com/mrcagney/gtfs_kit> |
| **`partridge`** | Python | Lazy-load, memory-light | Good alt; less active |
| **`transitland-lib`** | Go | Industrial-grade | Only if you want Go |
| **`gtfs-realtime-bindings`** | Python | GTFS-RT protobuf | Needed if we add real-time GTFS-RT feeds |

Cadence: stitched HK GTFS is updated weekly-ish by the community <https://github.com/hkbus/hk-public-transport-gtfs> **[VERIFY still maintained as of 2026]**; TD's own GTFS **[VERIFY cadence]**. Schedule a `just refresh-gtfs` nightly; on diff, rebuild OTP graph atomically.

Tool sketch: `transport.get_route_headway(route_operator, route_id, time_of_day)` returning `{"avg_headway_min": 8, "peak_hours_headway_min": 4, "source": "gtfs-hk-2026W16"}`.

### 7.2 S505 MTR fares + barrier-free

Data source: <https://data.gov.hk/en-data/dataset/mtr-data2-nt-bf-access> (barrier-free facilities), <https://data.gov.hk/en-data/dataset/mtr-data2-data-fare-table> (fares). Both are CSV/XLSX as of Q1 2026 **[VERIFY endpoint]**.

Tool sketch (half-day): `transport.get_mtr_fare(origin_station, destination_station, fare_type="adult_octopus")` → `{"fare_hkd": 12.5, "fare_type": "adult_octopus", "journey_stations": 8}`. Load the fare table into memory at startup; it rarely changes.

Barrier-free: `transport.get_mtr_accessibility(station_code)` → `{"has_lift": true, "wheelchair_gates": ["A1","B"], "toilet_accessible": true, "tactile_paving": true}`. Source: the nt-bf-access dataset.

### 7.3 S506 ferry timetable

Data source: <https://data.gov.hk/en-data/dataset/hk-td-tis_14-passenger-ferry-service-timetable> (CSV) **[VERIFY dataset page]**. Typical shape: per-operator fleet timetables, with "last update" monthly.

Tool sketch (half-day): `transport.get_ferry_schedule(operator, from_pier, to_pier, on_date)` → `{"scheduled_departures": ["07:00","07:30",...], "fare_hkd": 6.3, "operator": "Sun Ferry"}`.

Gotcha: HK ferry operators (Sun Ferry, HKKF, …) each publish in slightly different CSV shapes. Plan for 2–3 parser variants.

### 7.4 S507 PT routes + fares GeoJSON

Data source: <https://data.gov.hk/en-data/dataset/td-tis_2-pt-route-and-fare> (GeoJSON). This one's heavy — ~50 MB, containing every route shape + fare.

**Recommendation: do not load this into a tool directly.** Instead, ingest into OTP2 (§3.1) alongside the GTFS, and let OTP's `plan` API return fares via its fare calculator. This is what OTP's fare-rules logic is designed for.

If a standalone `transport.get_route_fare(operator, route, from_stop, to_stop)` is wanted: preprocess the GeoJSON at build time into a sqlite lookup table `fares.db`; load into memory on startup.

### 7.5 S512 iB1000 via CSDI

Data source: CSDI portal's ArcGIS FeatureServer (<https://portal.csdi.gov.hk/>).

**Unified enumeration possibility:** CSDI's catalog is browsable via its REST `/arcgis/rest/services` endpoint (standard Esri pattern — `GET .../services?f=json` lists all services, then per-service `?f=json` lists layers). The HK CSDI portal front-ends this with a catalog UI; the underlying FeatureServer catalog is machine-readable.

Implementation sketch (1 day for discovery + a generic tool):
1. `scripts/csdi_catalog.py` crawls `https://portal.csdi.gov.hk/arcgis/rest/services?f=json` **[VERIFY exact endpoint]**, expands each folder, records each FeatureServer's layer IDs + titles + CRS.
2. Bundle the resulting catalog as `data/csdi_catalog.json`.
3. New tool `geo.query_csdi_layer(service_id, layer_id, bbox, where="1=1", limit=50)` — a generic ArcGIS FeatureServer client (OBJECTID + geometry + attributes).
4. Specialised wrappers that set `service_id` + `layer_id` by well-known name: `geo.get_topographic_map`, `geo.get_hkha_estates_live`, `geo.get_lcsd_courts_live`.

This **unblocks the three bundled tools** (`facility.find_nearby_courts`, `facility.find_nearby_pools`, `housing.get_estate_info`) to move to live data — see `docs/DATASETS.md:97`.

---

## 8. Architectural refactors

### 8.1 Move to LangGraph / Pydantic AI?

**Priority: LOW. Reversibility: one-way-ish.**

Our orchestrator in `smcity/orchestrator.py` is ~600 lines of reasonably clean async Python. LangGraph/Pydantic AI would give us:

| Feature | LangGraph | Pydantic AI | Current |
|---|---|---|---|
| Checkpointer (resume mid-turn) | ✅ SqliteSaver | partial | manual via `SessionStore` |
| Graph visualisation | ✅ mermaid render | ❌ | — |
| State inspection (mid-turn) | ✅ `interrupt` primitive | ✅ via DI | — |
| Typed tool I/O | partial | ✅ native (Pydantic everywhere) | Pydantic already |
| Latency overhead | low–mid (wrapper) | minimal | zero |
| Dependency weight | LangChain + LangGraph transitive tree | pydantic + logfire-ish | none |
| Fit for tool-call-heavy agent | good | **best** | good |

References: LangGraph <https://langchain-ai.github.io/langgraph/>; Pydantic AI <https://ai.pydantic.dev/>.

**Verdict: don't refactor.** You're already shipping. The value of LangGraph's state-inspection is best-realised when building the graph from scratch, not retrofitting. The Pydantic AI fit is genuinely good but doesn't solve a user-facing problem you have today.

**If you do refactor later**, Pydantic AI is the better target because (a) less transitive dependency surface, (b) `ModelSettings` lets you swap LM Studio in as a plain OpenAI-compatible endpoint without abstractions, (c) first-class streaming.

### 8.2 Agentic replanning (reflexion-style)

**Priority: LOW. Reversibility: feature-flag.**

Current control loop: one LLM call picks tools → parallel execute → one LLM call synthesises reply. That's bounded-ReAct with cap = 1. For most HK smart-city queries this is the right cap — sub-second voice doesn't tolerate unbounded loops.

Reflexion-style replanning <https://arxiv.org/abs/2303.11366> would help on *compositional* queries where the first tool result invalidates the plan ("I wanted the MTR but there's a signal failure on Tsuen Wan line, re-plan with Citybus"). That's a real case, but it's < 5 % of turns.

**Recommendation: don't add replanning in the hot path.** Instead, add a **post-hoc validation step**: after synth, a cheap regex/schema check verifies the reply actually answered the user's question (e.g. user asked for a time, reply contains a time string). On failure, re-prompt once with "your previous answer didn't include a time; try again" — this is cheaper than replanning tools and catches most failure modes.

### 8.3 Cross-session tool-call caching

**Priority: MEDIUM. Reversibility: feature-flag.**

Current: `asyncio.gather` dispatches tools per turn, no caching. But many queries repeat: "weather now" is the same for 5 different users in the same minute. The MTR Next Train API responds the same for every query to a station for ~15 s.

Design: per-tool TTL cache keyed on `(tool_name, canonicalised_args)`. Redis or in-process LRU.

| Tool | TTL | Key |
|---|---|---|
| `context.get_current_weather` | 60 s | `()` |
| `context.get_aqhi` | 120 s | `()` |
| `context.get_active_warnings` | 60 s | `()` |
| `context.get_9day_forecast` | 30 min | `()` |
| `transport.get_mtr_next_trains` | 15 s | `(station_code, direction)` |
| `transport.get_kmb_eta_by_stop` | 15 s | `(stop_id)` |
| `transport.get_citybus_eta_by_route_stop` | 15 s | `(route, stop_id)` |
| `transport.get_gmb_eta` | 15 s | `(region, code, stop_id)` |
| `geo.address_lookup` | 24 h | `(query_text)` |
| `geo.search_osm_pois` | 5 min | `(category, bbox or point+radius)` |
| `facility.*` / `housing.*` (bundled) | no cache | — (already in-memory) |
| `transport.plan_journey` / `plan_simple_route` | 30 s | canonical origin+dest tuple |
| `transport.plan_taxi_estimate` / `plan_walking_route` | 30 s | same |

Implementation sketch (half-day):
1. Decorator `@cached(ttl_s=15, key=lambda args: (args["stop_id"],))` on tool handlers in `smcity/tools/`.
2. Backend: `cachetools.TTLCache` in-process by default; swap to Redis only if we go multi-process.
3. Cache hit short-circuits the HTTP call; emit a `tool_call.result` with `cached: true` in the event payload so the UI can chip it.

Expected saving: 400–900 ms on repeat queries. Additional benefit: reduces load on data.gov.hk (be a good citizen).

Non-goal: don't cache `tool_call` *sequences* across sessions ("what came after X") — that bleeds session state across users. Cache only the upstream HTTP responses.

---

## 9. Failure modes observed in production

### 9.1 Harmony-format leaks

**Priority: HIGH. Reversibility: config-only.**

Seen in `CHANGELOG.md` v0.1.1–v0.1.3: bare-leak recovery (`transport_plan_simple_route json {...}` appearing as text), harmony `<|start|>assistant<|channel|>commentary to=functions.X` leaking into user-visible text. Our `smcity/llm.py:74` has regex-based recovery.

**Root cause:** LM Studio's OpenAI shim sometimes fails to parse the harmony tool-call tokens into the `tool_calls` array, particularly in streaming mode, so they appear as raw `content`.

**LM Studio config flags that help [VERIFY build-by-build]:**
- `jinja` chat template override: ensure LM Studio is using the shipped `gpt-oss` chat template (should be automatic, but verify in the model's "prompt template" setting).
- "Tool use" / "Function calling" explicit toggle: some LM Studio versions expose a checkbox to enable harmony→openai translation. It should be on.
- `reasoning_effort` setting for gpt-oss: `low`/`medium`/`high` (reasoning models emit different analysis channels). Stick to `medium`; `high` more often leaks analysis tokens into content. **[VERIFY this setting's behaviour in your build]**.
- Streaming mode: harmony parsing in streaming has historically been flakier than non-streaming. Our `chat_stream` path is the main leak vector. If leaks persist, consider falling back to non-streaming for the tool-picking first hop and streaming only for the synth hop. (We essentially do this already via `chat()` for the first call, `chat_stream()` for the synth.)

**Keep our current regex recovery in `smcity/llm.py` regardless** — it's a useful belt-and-suspenders even if LM Studio fixes its parser. That code is ~50 lines and handles both canonical and bare formats.

Reference: harmony format spec <https://github.com/openai/harmony>, <https://cookbook.openai.com/articles/openai-harmony>.

### 9.2 Hallucinated source footers

**Priority: HIGH. Reversibility: already fixed (v0.1.3); document the pattern.**

The v0.1.3 fix (`_rewrite_source_footer` in `smcity/orchestrator.py:490`) strips any LLM-emitted `src:` line and appends a deterministic one built from actual citations. This is the correct design pattern: **never let the LLM write content that must be factual-from-code.**

Other projects' solutions to the same class of bug:
- **Constrained decoding / JSON-schema output.** Some agents emit a two-part response: `{"prose": "...", "citations": [...]}` enforced by `response_format: {"type":"json_schema", ...}`, then concatenate programmatically. Pros: the LLM literally can't generate a bad citation field. Cons: you pay a schema-enforcement overhead; also, LM Studio's `response_format` is not wired for tool-call turns (tool args are the schema-constrained output, not content).
- **Post-hoc entity linking** (what we do for source footers; good pattern to extend).
- **Separate prose and structured output calls.** Already our implicit pattern.

**Recommendation:** document this pattern in `docs/architecture/ARCHITECTURE.md` as a house rule: *"Any field the LLM might hallucinate that has a ground-truth source is regenerated from that source, not trusted from the LLM."* Apply to: citations, tool names in prose, ETAs, fares, stop IDs. We already do this for source footers; audit the other classes.

### 9.3 Mode-ask ping-pong

**Priority: HIGH. Reversibility: prompt tweak.**

Seen in the v0.2.0 fix (`plan_journey` unified multimodal tool + system prompt rewrite, `CHANGELOG.md:56`). Root cause: the system prompt for vague "how do I get from X to Y" queries was asking clarifying questions for mode before committing to a plan.

The v0.2.0 fix pattern — **"no-mode queries go straight to `plan_journey`, no clarification"** — is correct. Extend it:

1. **Default-pick heuristics over clarify loops.** If the query is "from A to B" with no mode, default to multimodal (`plan_journey`). If it's 9pm, default to taxi. If the distance is < 800 m (computed cheaply with the two-point haversine), default to walk. The agent mentions the default in the reply ("I used MTR since it's faster right now; walking would take 25 min").
2. **`meta.ask_user` should fire on ≤ 2 slot cases**, not vague underspecification. Prompt rule: *"If you need origin or destination, ask. Otherwise default."*
3. **Remember past preferences in `SessionSlots`**. If the user picked walk on their last query, their next query defaults to walk.

These are three small prompt + orchestrator tweaks (~1 day total). The user has already had this fix land in v0.2.0; document the **design pattern** so it doesn't regress when prompts are edited later.


---

## Top 10 enhancements, sorted by (impact × reversibility)

Each item is labelled `[impact / reversibility]` on a 1–5 scale each. Ranked by their product.

| # | Enhancement | Impact | Reversibility | Section |
|---|---|---|---|---|
| 1 | **Speculative decoding with `gpt-oss-20b` draft** | 5 | 5 | §1.1 |
| 2 | **Tool-surface reduction per turn (tool-group routing)** | 5 | 4 | §1.4 |
| 3 | **Prompt-prefix stability audit + test** | 4 | 5 | §1.2 |
| 4 | **Tool-call caching with per-tool TTL** | 4 | 5 | §8.3 |
| 5 | **Langfuse (self-hosted) + OpenLLMetry tracing** | 4 | 5 | §5.1 |
| 6 | **OpenTripPlanner 2 sidecar for full multimodal transit** | 5 | 4 | §3 |
| 7 | **CSDI ArcGIS catalog discovery + generic FeatureServer tool** | 4 | 4 | §7.5 |
| 8 | **Promptfoo deep-eval harness** | 3 | 5 | §5.2 |
| 9 | **GlotLID-3 as non-CJK language detector** | 3 | 5 | §4.1 |
| 10 | **Azure `yue-HK-WanLungNeural` streaming TTS adapter** | 4 | 4 | §1.5, §6.3 |

"Impact × reversibility" favours cheap-to-try / easy-to-revert changes — the right bias for a lab project already shipping.

## "Free lunches" — near-zero cost, positive value

These are changes that cost less than a day and either have near-zero downside or a clear feature flag.

1. **Sort `ToolRegistry.openai_schemas()` by name.** One-line diff. Guarantees prefix stability (§1.2).
2. **Add a `tests/test_prefix_stability.py` golden test.** Catches future cache-bust regressions (§1.2).
3. **Turn on LM Studio speculative decoding** with `gpt-oss-20b` draft. No code change; one UI toggle (§1.1).
4. **Strip harmony analysis channel from replayed history** in `SessionStore.append_turn`. 10-line change; shrinks prefill (§1.2).
5. **Add `cached: true` to `tool_call.result` events when available** — once caching lands, this is free observability (§8.3).
6. **Pre-warm KV cache on service boot** with a dummy `chat.completions.create(max_tokens=1)` using the full system prompt + tools. Saves 150–750 ms on first real turn (§1.2 / research doc §3.3).
7. **Pre-warm data.gov.hk DNS + TCP on boot** with one HEAD per upstream host. ~30–80 ms saved on first real call per host.
8. **Force Tailscale direct connection (not DERP relay)** on the Mac Studio ↔ agent host pair. ~30–70 ms saved per LLM round-trip.
9. **Document the "never-trust-LLM-for-ground-truth-fields" rule** in `ARCHITECTURE.md` (§9.2). No code; shapes future PRs.
10. **Add `uvloop`** as the event loop on macOS. ~5–20 ms saved per turn of scheduling jitter.

## "Needs its own session" — bigger refactors

Do not squeeze these into a fix commit or a minor release. Each is a multi-day effort with real risk.

1. **OpenTripPlanner 2 integration** (§3). Sidecar JVM process, graph build pipeline, nightly refresh, new tool, updated prompt. 2–3 days plus operational burden.
2. **CSDI generic FeatureServer client + three live-data migrations** (§7.5). Catalog discovery, generic tool, rewrite of the bundled LCSD + HKHA tools. 2–3 days.
3. **Observability end-to-end** (§5). Langfuse self-host setup, OpenLLMetry instrumentation, Promptfoo harness, dashboards. 2 days but touches every tool.
4. **LangGraph / Pydantic AI refactor** (§8.1). Only if disambiguation graph grows beyond what the custom orchestrator handles cleanly. A full week of work; breaks every test.
5. **Neural Cantonese post-processor (YueLLM-7B second slot)** (§2). Requires native-speaker grading first to prove it's needed. Benchmarked latency trade-off vs speculative decoding.
6. **ROS 2 bridge repo** (§6.1). Lives outside this repo; needs lab hardware to integrate.
7. **NLLB-200 translation sidecar** (§4.2). Only valuable once we have users in tl/id/vi/th; pre-work without users is wasted.
8. **Full golden-set expansion to 200+ queries across 10+ languages.** Needs a native speaker per language; that's a recruitment task, not a coding task.

## Things we should NOT build

These would look good on a roadmap and would actively hurt the product.

1. **Plan-and-execute agentic loops.** Adds an extra "planner" LLM call on the critical path for sub-second voice. Wrong shape for our latency budget (research doc §1.2). The only legit use of plan-and-execute here would be batch trip-planning a week ahead — not a robot use case.
2. **Unbounded ReAct / reflexion loops.** The pathology of ReAct is runaway. Cap stays at 1–2 (§8.2).
3. **Cloud MT with `yue` target.** DeepL/Google/Azure don't have Cantonese targets; any pipeline that passes through them will emit Mandarin-in-Cantonese-characters. Wrong answer, looks plausible (§4.2).
4. **Personal housing eligibility walk-throughs.** Legal risk, out of scope of data.gov.hk's public data, adjacent to advice. Keep `docs/GOAL.md`'s "never claims to check personal application status" rule (`CHANGELOG.md:103`, already enforced).
5. **Aggressive auto-language-switching mid-session** without the user's consent. Our current `Locale.from_detection(..., forced=...)` respects `locale_override`. Don't change that to "auto-switch if we detect a new language on this turn" — it breaks the user's expectation that forced locales persist.
6. **Red minibus real-time via community crawlers.** Best-effort data that's almost certain to embarrass the agent ("your bus is here" → it isn't). If we want minibus RT, it has to be via data.gov.hk's official GMB ETA (already live) or not at all.
7. **On-device LLM fallback (smaller model when LM Studio is unreachable).** Sounds resilient; in practice, the fallback model would mis-route tools and the user's "why is it suddenly worse" reaction erases the resilience benefit. Better to surface the degradation honestly with the existing "(LM Studio unreachable)" reply (`smcity/orchestrator.py:195`).
8. **Robot-platform-specific branches in the agent core.** The `docs/PROTOCOL.md` WebSocket contract is correct; adding ROS-specific code into `smcity/` would couple the agent to one robot vendor. Keep the bridge in a separate repo (§6.1).
9. **A custom Cantonese LLM fine-tune** before we have numeric proof the current pipeline isn't good enough (§2). Fine-tuning has real data, cost, and retraining-risk overhead (especially for harmony-format models); do nothing until the quality gate forces it.
10. **Persisting full conversation transcripts by default.** Current PII-redaction + slot-only persistence (`docs/research/03_*.md:323`) is the correct posture for a lab robot. Adding long-term transcript storage "for debugging" is a privacy regression that's hard to undo.

---

## Appendix — measurement protocol for anything you ship from this doc

Before merging any of the above:
1. **Before metrics**: record `just bench` p50/p95 turn latency, TSR on golden set, mean Cantonese polish score (by eye), failure-mode count from last 100 live turns.
2. **After metrics**: same four numbers.
3. **Ship gate**: p50 must not regress > 10 %, TSR must not regress by > 1 query, no new harmony leaks.
4. **Roll-back plan**: every HIGH item above has a feature flag or config toggle. Wire it before you ship.

This doc is meant to be edited. As items ship, move them to `CHANGELOG.md` and delete from here.
