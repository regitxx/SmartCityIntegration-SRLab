# 03 — Agentic Tool-Calling Architecture for the HK Smart-City Assistant

**Target hardware/runtime:** Mac Studio (Apple Silicon) running LM Studio, serving
`openai/gpt-oss-120b` over an OpenAI-compatible HTTP endpoint exposed on Tailscale.
**Product constraints:** conversational latency budget < ~1.2 s to first audio chunk,
multi-turn disambiguation, Cantonese-first, every data read goes through a tool that
wraps data.gov.hk APIs, eventual embodiment in a social robot.

**Knowledge window / citation policy.** The author did not have live WebSearch/WebFetch
available when drafting this document; claims are based on training data through
early 2026 and on the behaviour of the referenced libraries up to roughly Q1 2026.
Every URL below is the *canonical location* to verify the claim against; before you
pin a library version, re-open the linked page and confirm the behaviour still holds.
Items that are known to be fast-moving are flagged **VERIFY**.

---

## 0. TL;DR architecture

Use a **small, custom Python orchestrator** that speaks the OpenAI Chat Completions
API to LM Studio, uses **native function calling** as the primary tool-invocation
channel, falls back to **constrained JSON decoding** (via LM Studio's built-in
JSON-schema / grammar support) only when native tool calls misbehave, holds
**per-session slot state in a LangGraph-style typed dict** persisted to SQLite,
and exposes a **WebSocket + SSE** front door so the robot layer can plug in later
without touching the agent core. Observability via **Langfuse** (or OpenLLMetry →
OTLP) with self-hosted backend on the same Mac Studio.

The controller loop is **function-calling-first with a bounded ReAct fallback**
(max 2 reflective steps) — *not* plan-and-execute, which is too slow for voice.

---

## 1. LLM tool-calling patterns

### 1.1 The four schools, and which one wins for `gpt-oss-120b` on LM Studio

| Pattern | What it is | Works with gpt-oss-120b on LM Studio? | Latency | Reliability |
|---|---|---|---|---|
| **OpenAI-style function calling** (`tools=[…]`, assistant returns `tool_calls`) | Model emits a structured `tool_calls` array in its message; your runtime routes each call. | **Yes** — `gpt-oss` was trained with OpenAI's *harmony* response format and native tool-call tokens; LM Studio exposes them through the standard `/v1/chat/completions` `tools` field. **VERIFY** the LM Studio release notes for your installed version. | Best (one forward pass produces the call). | Very good on gpt-oss; this is the on-policy format. |
| **JSON-schema tool calling** (same wire format, but you rely on schema coercion) | Same as above, but the server enforces schema-validity during decoding. | **Yes** — LM Studio exposes `response_format: {"type":"json_schema", "json_schema":{…}}` (OpenAI-compatible) and also a llama.cpp-level `grammar` / `json_schema` parameter. **VERIFY**. | Small overhead (a few % for schema-constrained sampling). | Near-zero malformed JSON. |
| **External constrained decoding** — Guidance, Outlines, lm-format-enforcer, **XGrammar** | Client-side or server-side libraries force the logits to conform to a CFG/regex/JSON-schema. | Only if you run the model yourself (vLLM + XGrammar, TGI + outlines). **LM Studio does not expose its sampler to external constrainers.** You get the *built-in* grammar support, which is effectively lm-format-enforcer semantics. | XGrammar ~0 overhead (near-native throughput); outlines small overhead. | Highest reliability. |
| **Pure text + post-hoc parsing** (old ReAct-style "Action: …") | Parse free-form text. | Works but pointless when tool-calls are native. | Slowest (more tokens) and most brittle. | Worst. |

**Recommendation:** native OpenAI-style function calling as primary; switch the
same request to `response_format: json_schema` only for responses where the model
has decided *not* to call a tool and must emit a user-facing structured payload
(e.g., a route summary for the TTS layer). Do not attempt to bolt XGrammar/Outlines
on top of LM Studio — you can't reach the sampler.

Verify live:

- LM Studio API surface: <https://lmstudio.ai/docs/app/api> and
  <https://lmstudio.ai/docs/app/api/endpoints/openai> (**VERIFY** grammar / JSON-schema support).
- llama.cpp server grammar + `json_schema`: <https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md>.
- gpt-oss harmony format: <https://github.com/openai/harmony> and
  <https://cookbook.openai.com/articles/openai-harmony>.
- OpenAI function calling spec: <https://platform.openai.com/docs/guides/function-calling>.
- XGrammar: <https://github.com/mlc-ai/xgrammar>.
- Outlines: <https://github.com/dottxt-ai/outlines>.
- lm-format-enforcer: <https://github.com/noamgat/lm-format-enforcer>.
- Guidance: <https://github.com/guidance-ai/guidance>.

### 1.2 Single-turn vs multi-turn loops; ReAct vs plan-and-execute

For voice-driven conversational latency, the ranking is unambiguous:

1. **Function-calling-only, one tool hop per user turn (preferred).** The model
   sees the user message, calls zero or one tool, you return the tool result,
   the model answers. One to two forward passes per user turn.
2. **Bounded ReAct (cap = 2 reflection steps).** Used when a query legitimately
   needs composition ("traffic *and* weather on the route"). Keep the cap hard —
   ReAct's killer is runaway loops.
3. **Plan-and-execute.** Good for batch / long-horizon tasks (trip planning a
   week ahead); **wrong** for sub-second voice turns because it adds an extra
   "planner" LLM call on the critical path. Keep it out of the v1 hot path.

ReAct reference: Yao et al. 2022, <https://arxiv.org/abs/2210.03629>.
Plan-and-execute reference: LangGraph's own write-up,
<https://blog.langchain.dev/planning-agents/>.

### 1.3 Parallel tool calls

OpenAI's `parallel_tool_calls: true` tells the model it *may* emit multiple
`tool_calls` in one assistant message (e.g. fetch weather + AQI simultaneously).
Whether this forwards correctly through LM Studio depends on two things:

1. **Does gpt-oss emit multiple tool calls in one turn?** Yes — the harmony
   format supports a list of calls, and OpenAI's reference integration honours
   it. **VERIFY** against the model card: <https://huggingface.co/openai/gpt-oss-120b>.
2. **Does LM Studio's OpenAI shim preserve the array?** As of Q1 2026, LM Studio
   surfaces the full `tool_calls` array in streamed and non-streamed responses.
   **VERIFY** on your installed build (the regression is easy to spot: only the
   first call makes it to the client).

**Recommendation:** enable `parallel_tool_calls=True` and fan out execution with
`asyncio.gather`. For our tools (weather, AQI, traffic, MTR status) this alone
saves 200–400 ms on the "how do I get from A to B right now" query.

---

## 2. Orchestration frameworks

### 2.1 Head-to-head

| Framework | Latency (overhead/turn) | Lock-in | Debuggability | Robotics integration | Notes |
|---|---|---|---|---|---|
| **LangGraph** (LangChain) | Low–med. Graph runtime is light, but the LangChain `ChatOpenAI` wrapper adds a few ms and sometimes hidden retries. | Medium. Your nodes talk to LC primitives, but you can call the OpenAI SDK directly inside a node. | **Best in class.** Built-in state inspection, checkpointers, LangSmith tracing. | Good — graphs serialize, state is explicit, easy to snapshot between dialog turns. | `StateGraph` with typed `TypedDict` state is a near-perfect match for slot-filling. |
| **LlamaIndex Agents** (AgentWorkflow, FunctionAgent) | Low. | Medium — tool abstractions are LI-specific. | Good; workflow events are introspectable. | OK. | Strongest when RAG dominates; overkill if most work is tool calls against HTTP APIs. |
| **OpenAI Agents SDK** (`openai-agents`) | Very low — it's a thin wrapper around Responses/Chat APIs. | **High** on paper but actually low: the library targets OpenAI-compatible endpoints; it works against LM Studio with `OpenAI(base_url=…)`. **VERIFY** tracing hooks. | Good (hosted traces on OpenAI, or route to local OTEL). | OK. | Clean handoff / guardrails model; weak on long-running graph state. |
| **Pydantic AI** | **Lowest** — tiny, type-first, zero LangChain baggage. | Low. | Very good; everything is a typed function with structured outputs. | Good — easy to wrap in any transport. | Fast-moving library; excellent fit for a tool-heavy voice agent. |
| **Anthropic Agent SDK** (`claude-agent-sdk`) | Low. | High against Anthropic's native API; it does *not* target arbitrary OpenAI-compatible endpoints. | Good. | Medium. | Not a fit here — you're not running Claude. |
| **Custom lightweight Python** (`openai` SDK + asyncio + typed state machine) | **Lowest possible.** | None. | As good as you build. | Best — you control every seam. | ~400 lines gets you slots, parallel tool exec, streaming, retries. |

### 2.2 Recommendation

**Primary:** **custom lightweight orchestrator** built on the `openai` Python SDK
(pointing at LM Studio), using `pydantic` models for tool schemas and session
state, and a `StateGraph`-style explicit slot state machine. You get the lowest
latency, zero lock-in, and a clean seam for a future robotics transport.

**Secondary (if the team wants batteries):** **LangGraph**, because the
checkpointer + state-inspection story is genuinely good for multi-turn
disambiguation and you will want those traces when the robot misroutes someone
to Sha Tin via the wrong tunnel at 2 AM.

**Do not adopt:** Anthropic Agent SDK (wrong vendor), LlamaIndex Agents (RAG-
centric, and we are mostly tool-calling).

Links:

- LangGraph: <https://langchain-ai.github.io/langgraph/>
- LlamaIndex AgentWorkflow: <https://docs.llamaindex.ai/en/stable/module_guides/deploying/agents/>
- OpenAI Agents SDK: <https://openai.github.io/openai-agents-python/>
- Pydantic AI: <https://ai.pydantic.dev/>
- Anthropic Agent SDK: <https://github.com/anthropics/claude-agent-sdk-python>

---

## 3. Latency-critical design

### 3.1 Prompt caching against llama.cpp / LM Studio

llama.cpp's server (which LM Studio wraps) maintains a **per-slot KV cache** and
does **automatic longest-prefix matching** across requests to the same slot. To
maximise cache hits:

1. **Pin the system prompt.** Byte-for-byte stable. Any field that changes per
   turn (timestamp, weather, user locale) goes *after* the stable preamble,
   ideally in the last user turn or as a tool result — not in the system prompt.
2. **Stable tool-schema ordering.** Sort `tools=[…]` deterministically (e.g. by
   name). The schema is serialised into the prompt; reordering busts the cache.
3. **Stable tool-schema content.** Do not regenerate JSON-schema descriptions on
   every request (Pydantic `.model_json_schema()` is deterministic but beware
   `additionalProperties` and `$ref` inlining differences across versions).
4. **One `session_id` → one llama.cpp slot.** Use the `slot_id` / `id_slot`
   parameter (or the OpenAI `user` field, which many llama.cpp builds route to
   a slot) to keep a given conversation on the same slot so its KV state
   persists. **VERIFY** on your LM Studio build:
   <https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md>.
5. **Keep the conversation short, the context long.** Truncate prior assistant
   thoughts (not user utterances) aggressively; the harmony format already
   distinguishes analysis channels from final output.

A warm KV cache on a 120 B MoE running on a Mac Studio typically cuts
time-to-first-token from ~900 ms to ~150 ms on a repeated prefix. **VERIFY** on
your rig — the M-series unified-memory throughput dominates.

### 3.2 Streaming tool-call arguments while still enforcing schemas

OpenAI's streaming format emits `tool_calls[i].function.arguments` as **token
deltas**. You have two choices:

- **Parse-on-complete.** Accumulate deltas, parse JSON once `finish_reason=tool_calls`
  arrives. Simplest; you lose ~50–150 ms you could have spent dispatching.
- **Incremental partial-JSON parsing.** Use a streaming JSON parser
  (`json-stream`, `partial-json-parser`, or the one built into Pydantic AI /
  Instructor) to extract well-formed sub-objects early. Dispatch a tool the
  moment its arguments are complete, *before* siblings in a parallel call finish.

For a Cantonese voice loop, **incremental parsing is worth the complexity only
if you routinely emit parallel tool calls**; otherwise parse-on-complete.

If you want hard schema enforcement on the server side, use LM Studio's
`response_format: {"type":"json_schema", ...}` — but note this is typically only
applied to the *assistant content*, not to the `tool_calls` arguments (those are
already schema-constrained by the `tools` definition). **VERIFY** in the LM
Studio docs for your version.

Refs:

- OpenAI streaming + tool calls: <https://platform.openai.com/docs/guides/function-calling#streaming>
- `json-stream`: <https://github.com/daggaz/json-stream>
- Instructor (partial streaming): <https://github.com/567-labs/instructor>

### 3.3 Pre-warming / keep-alive

Cold-start pain on LM Studio has two sources: **model load** (solved by keeping
the model pinned in memory — LM Studio's "keep model loaded" toggle or its
`/v1/models` preload) and **KV-cache cold start** (solved by warming the
conversation prefix).

Concrete recipe:

1. On service boot, send a dummy `chat.completions.create` with your full
   system prompt + tool schemas + a single user `"ping"`, `max_tokens=1`,
   `temperature=0`. This loads the model *and* primes the KV cache for the
   stable prefix.
2. Run a cron / asyncio task that re-sends this ping every N minutes (LM
   Studio's default TTL for idle models varies by version; **VERIFY**).
3. Use HTTP keep-alive on the client (`httpx.AsyncClient` with a single shared
   connection pool). Do not open a new TCP connection per turn.
4. Over Tailscale, DERP-relay fallback adds ~50 ms. Ensure the Mac Studio and
   the agent host are **direct-connected** (`tailscale status` shows
   `direct`, not `relay`).

### 3.4 Speculative decoding, drafts, and cheap-classifier short-circuits

Three complementary tricks, in increasing order of ROI for this product:

1. **Draft-model speculative decoding.** llama.cpp supports
   `--model-draft` / server `draft_model` with a smaller model of the same
   tokenizer family. For `gpt-oss-120b`, the natural draft is `gpt-oss-20b`;
   LM Studio exposes a "speculative decoding" toggle in recent builds (**VERIFY**:
   <https://lmstudio.ai/docs>). On an M-series Mac this typically yields
   1.5–2.2× tokens/sec on easy generations with ~zero accuracy loss.
2. **Pre-classification.** A tiny local model (e.g. a 0.5–2 B instruction-tuned
   model, or even a fastText / XLM-R classifier) routes the utterance to one
   of {chitchat, route_query, weather_query, disambiguation_response, other}.
   For the first three you can often *skip the 120 B model entirely* or call
   a single tool directly. Expected saving: 300–700 ms on the easy 60 % of
   turns.
3. **Intent cache.** Hash `(last user turn, current slot state)` → cached
   tool-call plan. TTL = 60 s. Catches repeated "again?" and "and the return
   trip?" turns cheaply.

Refs:

- Speculative decoding: Leviathan et al. 2022, <https://arxiv.org/abs/2211.17192>.
- llama.cpp speculative docs: <https://github.com/ggml-org/llama.cpp/tree/master/examples/speculative>.

---

## 4. Disambiguation patterns

### 4.1 Clarify vs best-guess: cost-of-being-wrong

Formalise a **clarification-cost function** per slot:

```
clarify_if( entropy(slot) * cost_of_wrong(slot) > cost_of_asking )
```

- `entropy(slot)` — low if the NLU layer pinned a unique value; high if
  multiple candidates scored close.
- `cost_of_wrong(slot)` — routing the user down the wrong MTR line and making
  them ask again is ~5 s + user annoyance. Giving the weather for Sha Tin when
  they meant Sheung Wan is ~1 s.
- `cost_of_asking` — ~1.5 s of voice turnaround.

**Heuristics that work:**

- Origin/destination ambiguity with >1 plausible candidate: **always clarify**
  (cost-of-wrong is high; users forgive a confirmation).
- Mode (bus/MTR/taxi) with no prior preference and no contextual signal
  (rain, AQI, time of day): **clarify once**, then remember the preference
  in session memory.
- Time (“now” vs “at 6pm”): **best-guess "now"** and mention it in the
  response ("Right now, the MTR is your fastest option"). Cheap to be wrong.

### 4.2 Explicit slot state

Encode slots as a typed object the agent carries turn-to-turn. Example:

```python
class RouteIntent(BaseModel):
    origin: StopRef | None = None
    destination: StopRef | None = None
    mode: Literal["mtr","bus","taxi","walk","any"] | None = None
    depart_at: datetime | None = None  # None == "now"
    constraints: list[Literal["cheapest","fastest","fewest_transfers","step_free"]] = []
    confirmed: bool = False
```

Two design rules:

1. **The slot object is the source of truth, not the chat history.** Tool calls
   read from / write to it. The LLM sees a compact JSON snapshot of current
   slots at the end of each turn's system context (outside the cached prefix —
   see §3.1).
2. **Clarification is itself a tool.** Expose `ask_user(question, slot_name,
   candidates)` as a "tool" the model can call. This unifies the control loop
   (everything is a tool call) and makes the policy trainable / observable.

This is a well-worn pattern; good references:

- RavenClaw / TrindiKit dialogue-management tradition (information state).
- Rasa's "forms" and "active loops": <https://rasa.com/docs/rasa/forms/>.
- LangGraph's `interrupt` primitive for human-in-the-loop clarifications:
  <https://langchain-ai.github.io/langgraph/concepts/human_in_the_loop/>.

---

## 5. Session memory

### 5.1 Options

| Option | Latency | Persistence | Queryability | Ops burden | Fit |
|---|---|---|---|---|---|
| In-process dict | ~0 | Lost on restart | None | None | Dev only. |
| SQLite (WAL) | ~0.1–1 ms | Yes | SQL | None (single file) | **Best default for single-robot deployment.** |
| Redis | ~0.3–1 ms local | Yes (AOF) | Key/value + streams | One service to run | Needed if you go multi-process. |
| LangGraph `SqliteSaver` / `PostgresSaver` checkpointer | wraps the above | Yes | LangGraph-aware | None extra | Use iff you adopt LangGraph. |

### 5.2 Recommendation

- **SQLite + WAL** on the agent host, one table for sessions, one for turns,
  one for slot snapshots. Use `apsw` or `aiosqlite` for async.
- **Serialization:** `msgspec` (fastest), or `pydantic.model_dump_json` if you
  want the ecosystem. Avoid pickle (supply-chain + version fragility).
- **TTL:** 24 h for conversational memory, 30 d for long-term preferences
  (favourite MTR stops, preferred mode), configurable per user.

### 5.3 Privacy hygiene

- **Do not persist raw user audio or transcripts by default.** Store slot
  values and a hashed conversation id.
- **PII redaction at ingress.** Run a small regex + NER pass on user utterances
  before they go into memory (phone numbers, HKID, addresses).
- **Per-user delete endpoint.** Robot context: users need a physical
  "forget me" gesture the robot honours. Implement as a single SQL
  `DELETE FROM sessions WHERE user_id = ?` plus cache flush.
- GDPR / PDPO (HK Personal Data (Privacy) Ordinance) alignment: store the
  lawful-basis flag and consent version with each session row. Ref:
  <https://www.pcpd.org.hk/english/data_privacy_law/ordinance_at_a_Glance/ordinance.html>.

---

## 6. Robustness + safety

### 6.1 Input validation at tool boundaries

Every tool is a Pydantic `BaseModel` for inputs and outputs. Validate *both*
directions:

- **Inputs:** reject any tool call with arguments that don't match schema,
  return a structured `ToolError` back to the model so it can self-correct.
- **Outputs:** after calling data.gov.hk, coerce into the declared output
  schema. If coercion fails, do not pass free-form text back to the LLM —
  return a typed error.

### 6.2 Output validation (no hallucinated bus stops)

Hallucinated entities are the single biggest correctness risk. Mitigations:

1. **Never let the LLM emit an entity ID.** Tool outputs carry `stop_id`,
   `route_id`, etc.; the LLM only references them by name in prose. Any
   downstream action (robot actuator, map pin) reads IDs from the tool
   result, not from the LLM text.
2. **Entity-linker post-check.** If the final response mentions a named stop,
   grep it against the known gazetteer; if not found, regenerate with a
   corrective system message.
3. **Grounding citations.** Every factual claim in the reply carries a
   `source_tool_call_id`. Unsourced claims are stripped.

### 6.3 Circuit breakers and retries

- Wrap each tool in a circuit breaker (`pybreaker` or `purgatory`) with
  failure threshold 5 / 30 s, open-state fallback to a cached last-known-good
  result where safe (weather: yes; real-time traffic: no, degrade gracefully
  and tell the user).
- Retries: exponential backoff with full jitter, max 2 retries, per-tool
  total timeout < 700 ms (leaves budget for the LLM second pass).
- Bulkhead: separate asyncio semaphores per upstream host so a slow MTR API
  can't starve weather calls.

Refs: Hystrix/resilience4j patterns, `purgatory`
(<https://github.com/mardiros/purgatory>), `tenacity`
(<https://github.com/jd/tenacity>).

### 6.4 Observability

| Tool | Strength | Weakness | Verdict |
|---|---|---|---|
| **Langfuse** (self-hostable) | LLM-native traces, prompt versioning, eval hooks. Docker compose on the Mac Studio works. | Yet another service. | **Pick this** as the primary LLM trace store. |
| **Phoenix (Arize)** | Strong tracing UI, OpenInference. | Heavier footprint. | Good alternative to Langfuse. |
| **OpenLLMetry** (Traceloop) | Standards-based (OpenTelemetry semconv for GenAI). | Less opinionated UI. | Use as the **instrumentation library**, export to Langfuse or any OTLP backend. |
| **Raw OpenTelemetry** | Ubiquitous. | You write the LLM semantics. | Use for everything non-LLM. |

**Recommended:** instrument with **OpenLLMetry** (semantic conventions) +
OpenTelemetry for HTTP/DB/tool spans, export OTLP to **Langfuse** and to a
local Grafana Tempo if you already run one.

Links:

- Langfuse: <https://langfuse.com/docs>.
- Arize Phoenix: <https://github.com/Arize-ai/phoenix>.
- OpenLLMetry: <https://github.com/traceloop/openllmetry>.
- OTel GenAI semconv: <https://opentelemetry.io/docs/specs/semconv/gen-ai/>.

---

## 7. Integration-friendliness

### 7.1 Transport-agnostic service

Expose **one internal async core** and multiple thin transports:

```
AgentCore.handle_turn(session_id, user_input) -> async iterator[AgentEvent]
```

Where `AgentEvent` is a sealed union: `TokenDelta`, `ToolCallStarted`,
`ToolCallResult`, `ClarificationRequested`, `FinalResponse`, `Error`.

Wrap this with:

- **WebSocket** (`fastapi.WebSocket` / `starlette`): primary transport for the
  robot. Full-duplex, streaming, low-latency.
- **SSE** (`/v1/chat/stream`): for browser clients and simple CLI tools.
- **REST** (`/v1/chat`): non-streaming, for batch / smoke tests.
- **gRPC** (optional, later): only if the robotics stack is ROS 2 + gRPC
  native. Use the same `AgentEvent` proto.

### 7.2 Swappable tool layer

Define tools as:

```python
class Tool(Protocol):
    name: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    async def run(self, args: BaseModel, ctx: ToolContext) -> BaseModel: ...
```

Load them from entry points (`importlib.metadata.entry_points(group="hkagent.tools")`)
so data.gov.hk tools live in one package, robot-actuator tools (later) live in
another, and the core agent depends on neither.

For the robot future: add a `RobotTool` kind whose `run` posts to a ROS 2
action / topic bridge. The agent core never learns about ROS.

---

## 8. Recommended architecture (ASCII)

```
                        +-------------------------------------------+
                        |            Client transports              |
                        |  WebSocket | SSE | REST | (gRPC later)    |
                        +---------------------+---------------------+
                                              |
                                              v
                        +-------------------------------------------+
                        |              AgentCore (async)            |
                        |                                           |
                        |  +-------------------------------------+  |
                        |  |  Turn controller                    |  |
                        |  |   - pre-classifier (tiny model)     |  |
                        |  |   - bounded ReAct loop (cap=2)      |  |
                        |  |   - parallel tool dispatch          |  |
                        |  +-----+-------------------+-----------+  |
                        |        |                   |              |
                        |        v                   v              |
                        |  +-----------+       +-----------+        |
                        |  |  Slot SM  |<----->|  Memory   |        |
                        |  |  (Pydantic|       |  SQLite   |        |
                        |  |  state)   |       |  (WAL)    |        |
                        |  +-----+-----+       +-----------+        |
                        |        |                                  |
                        |        v                                  |
                        |  +-------------------------------------+  |
                        |  |  Tool registry (entry points)       |  |
                        |  |  - data.gov.hk: transit, weather,   |  |
                        |  |    AQI, traffic                     |  |
                        |  |  - ask_user (clarification)         |  |
                        |  |  - robot.*  (later)                 |  |
                        |  +-----+-------------------+-----------+  |
                        |        |                   |              |
                        +--------|-------------------|--------------+
                                 |                   |
                                 v                   v
                       +-------------------+  +--------------------+
                       | data.gov.hk HTTP  |  |  LM Studio         |
                       | (httpx + breaker) |  |  /v1/chat/completions|
                       |                   |  |  gpt-oss-120b       |
                       |                   |  |  + speculative draft|
                       |                   |  |  (gpt-oss-20b)      |
                       +-------------------+  +--------------------+

               Observability: OpenLLMetry -> OTLP -> Langfuse (self-hosted)
```

---

## 9. Recommended stack (one-liners)

- **LLM runtime:** LM Studio, `gpt-oss-120b` + `gpt-oss-20b` draft, speculative decoding on. *Native harmony tool-calling is on-policy; draft model nets 1.5–2× tok/s.*
- **Client SDK:** `openai` Python SDK with `base_url=http://<tailscale-ip>:1234/v1`. *Zero lock-in, streaming + tool calls out of the box.*
- **Orchestrator:** custom async Python core (~400 LOC), option to swap to LangGraph if the team wants graph tooling. *Lowest overhead; clean integration seam.*
- **Schemas:** Pydantic v2 for tool IO and slot state. *Deterministic JSON-schema, msgspec-fast.*
- **Memory:** SQLite WAL via `aiosqlite`, `msgspec` serialization. *Single file, no ops, fast enough.*
- **Transport:** FastAPI + WebSocket + SSE. *Robot consumes WS; dev tools use SSE.*
- **Observability:** OpenLLMetry instrumentation → OTLP → Langfuse (self-hosted on the Mac Studio). *Standards-based, LLM-aware UI.*
- **Resilience:** `tenacity` for retries, `purgatory` for circuit breakers, per-tool timeouts. *Keeps voice turns bounded even when upstreams flake.*
- **Safety:** Pydantic input/output validation + entity-linker post-check + grounding citations. *No hallucinated bus stops.*
- **i18n:** detect zh-HK / yue vs en at transport layer, pass `locale` into every tool call, let the LLM reply in the user's language. *Cantonese-first without model surgery.*

---

## 10. Micro-optimisations that shave 50–500 ms per turn

Numbered roughly in order of ROI on *this* rig:

1. **Pin the model in memory** in LM Studio ("Keep model loaded"). Saves 2–30 s on a cold turn.
2. **Keep-alive ping** with the full system prompt + tool schemas every 2–5 min. Saves ~150–750 ms of prefix KV-cache rebuild.
3. **Deterministic tool-schema ordering** (sort by name). Avoids silent cache busts on deploys. Saves 100–400 ms when it hits.
4. **Stable system prompt, volatile context last.** Put "Current time: …" and per-turn locale *after* the cached prefix. Saves 100–300 ms.
5. **One `session_id` → one llama.cpp slot** via the OpenAI `user` field. Keeps the per-conversation KV hot. Saves 100–300 ms per follow-up turn.
6. **`parallel_tool_calls=True`** for independent tools (weather + AQI + traffic). Saves 200–400 ms on compound queries.
7. **Speculative decoding** with `gpt-oss-20b` draft. Saves 30–50 % on generation tokens → typically 150–400 ms on a 120-token answer.
8. **Pre-classifier** (tiny local model / fastText) that short-circuits chitchat and single-tool intents. Saves 300–700 ms on ~60 % of turns.
9. **Incremental JSON parsing** of tool arguments. Saves 50–150 ms on parallel-tool turns; do this only if #6 is heavily used.
10. **HTTP keep-alive + shared `httpx.AsyncClient`.** Saves ~20–60 ms per upstream call.
11. **Warm the data.gov.hk DNS + TCP** on boot (one HEAD per host). Saves ~30–80 ms on first real call.
12. **Tailscale direct connection** (not DERP relay). Saves ~30–70 ms.
13. **`temperature=0` + `top_p=1` + `max_tokens` tight** for tool-calling turns. Saves 50–150 ms of tail tokens.
14. **Strip the model's "analysis" channel** from the context you send back next turn (harmony format lets you keep only the final message). Saves 50–200 ms of prefill on turn N+1.
15. **Cap ReAct reflections at 2.** Saves unbounded time when the model loops.
16. **Intent/tool-result cache** keyed on `(user_utterance_hash, slot_state_hash)`, 60 s TTL. Saves 400–900 ms on "again?" / "and the return?".
17. **Pre-compile Pydantic models** (`model_rebuild()` at import time). Saves 5–20 ms per turn, but adds up at 10 Hz.
18. **Avoid re-serializing tool schemas** per request; cache the JSON string. Saves 3–10 ms.
19. **One shared asyncio loop**, `uvloop` on macOS. Saves 5–20 ms of scheduling jitter.
20. **Stream to TTS in chunks at clause boundaries**, not at EOS. This is a TTS-side win, but it's where 300–800 ms of "perceived" latency actually lives — budget for it.

---

## 11. Open questions / things to VERIFY before locking the design

- Exact LM Studio build's support for `parallel_tool_calls`, `response_format: json_schema`, and grammar/`json_schema` passthrough to llama.cpp. Re-check <https://lmstudio.ai/docs/app/api/endpoints/openai>.
- Whether `gpt-oss-120b` on your quantisation (likely MXFP4 or Q4_K_M) preserves tool-call reliability vs the reference checkpoint. Sanity-check on a 200-prompt tool-calling eval before shipping. Model card: <https://huggingface.co/openai/gpt-oss-120b>.
- Whether LM Studio exposes a slot-pinning knob on your version (OpenAI `user` field → llama.cpp slot mapping has been inconsistent historically).
- Real measured draft-acceptance rate with `gpt-oss-20b` as draft for `gpt-oss-120b`; harmony tokens should align but measure it.
- Cantonese handling: gpt-oss's Cantonese fidelity is decent but not first-rate; evaluate on a HK-accented transcription set before committing to monolingual prompting.
