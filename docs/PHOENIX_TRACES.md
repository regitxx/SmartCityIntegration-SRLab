# Reading Phoenix Traces

A short guide to what you see in the Phoenix UI for this project. Written
for stakeholders who need to interpret a screenshot, not for developers
who are debugging.

## The two kinds of spans you will see

Phoenix records a "span" for every interesting thing the agent does in
one turn. We rely on two auto-instrumentations:

| Source                                    | What it captures                                                              |
|-------------------------------------------|-------------------------------------------------------------------------------|
| `openinference-instrumentation-openai`    | Every LLM call (gpt-oss-120b on the Mac Studio). Shown as `ChatCompletion`.   |
| `opentelemetry-instrumentation-httpx`     | Every outbound HTTP request. Shown as `GET` or `POST` with `kind: unknown`.   |

Both fire automatically — we did not have to write per-call tracing code.

## "Why are there 145,008 empty GET spans?"

Those are not empty — they are *outbound HTTP calls*. Every time the
agent fetches live data, the httpx instrumentation creates a span:

- KMB / Citybus / GMB ETA endpoints on `data.gov.hk`
- MTR next-train endpoint
- ALS geocoder for landmark lookup
- OpenStreetMap Overpass for POI search
- HKO weather + warnings + AQHI feeds
- LCSD facility CSDI FeatureServer

They look "empty" in the Phoenix list view for two structural reasons:

1. **`kind: unknown`** — httpx instrumentation tags the span as a generic
   HTTP CLIENT. It does not know whether the request is for an MTR ETA or
   a weather warning; that semantic tagging is OpenInference-specific and
   only applies to LLM spans. Phoenix renders any non-LLM, non-tool span
   as `unknown`.
2. **`input / output: --`** — request and response bodies are deliberately
   not recorded. Bodies often contain user-typed addresses or coordinates,
   and stuffing them into every span would leak PII into Phoenix.

The useful signal on these spans is **latency**. Typical healthy ranges:

| Upstream                          | Healthy P50 | Watch above |
|-----------------------------------|-------------|-------------|
| MTR / KMB / Citybus ETA           | 100-300 ms  | 1 s         |
| ALS geocoder                      | 200-500 ms  | 2 s         |
| Overpass POI search               | 1-3 s       | 8 s         |
| HKO weather / warnings            | 50-200 ms   | 1 s         |

## What one `smcity.turn` actually represents

A `smcity.turn` is the root span for one user-message → bot-reply cycle.
Everything else nests under it. The lifecycle:

```
smcity.turn (root)
├── ChatCompletion        # LLM decides which tools to call
├── tool.geo.address_lookup        # if landmark resolution needed
├── tool.transport.find_stops_near_point   # parallel with other tools
├── tool.transport.get_kmb_eta_by_stop     # parallel with other tools
├── ChatCompletion        # synthesis: LLM writes the user-facing reply
└── (optional) ChatCompletion   # retry if a gate / invariant fired
```

The root span carries the human-readable attributes you can show a
stakeholder without opening any child span:

```
user.text           the question the user asked
reply.text          what the agent answered (truncated to 1KB)
session.id          per-session id (changes when the user refreshes)
detected_lang       primary language detected (zho, yue, eng, …)
locale_override     if the user forced a locale via the UI
tool_count          how many tools the agent ran on this turn
citations_count     how many sources the reply cites
```

## A worked example: failure mode

The trace `f1c5881a1f167ecf` (54.3s, ~20K tokens) is a useful example of
what a *failed* turn looks like. The user asked, in Traditional Chinese:

> 從海怡半島到鄰近區域，最近一班公車的到達預測是多久？
> *("From South Horizons to nearby areas, how long until the next bus arrives?")*

The trace shows:

1. **2.5s** — first `ChatCompletion`. LLM picks tools.
2. **78ms** — `tool.geo.address_lookup` resolves "海怡半島" to coords.
3. **7ms** — `tool.transport.find_stops_near_point` returns the bus stops
   near those coords.
4. **18.3s** — `ChatCompletion` synthesis. LLM should write the answer
   here. It did not — it produced no prose.
5. **19s, 8.2s, 6s** — three more `ChatCompletion` retries triggered by
   our `chain_rules` and `synthesis_invariants` engines. Each failed the
   same way.
6. The orchestrator gave up and returned the user-facing fallback:
   `"(I couldn't compose an answer — try rephrasing?)"`

Why we surface failures this way rather than crashing:

- The agent **did** call the right tools and **did** get real data.
- The failure was synthesis-only — the LLM understood the question, used
  the data, then emitted only tool-call tokens with no natural-language
  reply. The orchestrator's fallback text is preferable to silence.
- The trace is preserved so an operator can replay the inputs against a
  newer model.

## Cost interpretation

The `Total Cost` figure at the top of the Phoenix dashboard
(\$12.52 across 145,008 spans) is the cumulative dollar estimate Phoenix
computes from token counts. Since we self-host gpt-oss-120b on the Mac
Studio, the actual cash cost is electricity. The dollar figure is
useful as a *relative* signal — a 10× cost jump on a quiet day means
something is calling the LLM in a loop.

## Where to dig further

- For an individual slow / failed turn: open the trace, look at the
  `ChatCompletion` durations, then click into the longest one to see
  the prompt + completion + token breakdown.
- For systemic slowness: filter spans by `latency > 5s` and group by
  `name`. Repeated slow `tool.*` spans point at a flaky upstream;
  repeated slow `ChatCompletion` points at the LLM.
- For traffic-shape questions ("how many turns yesterday?"): use the
  Sessions tab in Phoenix — it groups by `session.id` automatically.
