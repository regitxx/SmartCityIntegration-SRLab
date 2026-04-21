# WebSocket Protocol — `/ws/:session_id`

**Version:** v0.1.0 · 2026-04-21
**Consumer contract:** the lab's future robotics platform should target this file. No breaking changes without a major-version bump.

---

## Connection

```
ws(s)://<host>:8080/ws/<session_id>
```

- `session_id` is chosen by the client; any URL-safe string of 1-128 chars.
- The same `session_id` should be used for the whole conversation so the orchestrator keeps state (slots + locale + LM Studio KV cache) warm.
- No auth at the transport layer in v0.1 — the service listens on the Tailscale tailnet only. External exposure via Tailscale Funnel is opt-in.

## Message framing

Every frame is a single JSON object with a `type` field. Unknown types are ignored by both sides (forward-compatible extension). UTF-8 only.

---

## Server → client events

### `ready` — sent once on connect

```json
{
  "type": "ready",
  "session_id": "abc123",
  "model": "openai/gpt-oss-120b",
  "version": "0.1.0",
  "ts": "2026-04-21T10:24:56.077158Z"
}
```

### `locale_set` — ack for a `set_locale` command

```json
{ "type": "locale_set", "locale": "yue", "at": "2026-04-21T10:25:01.012Z" }
```

### `turn.start` — a user turn is starting

```json
{
  "type": "turn.start",
  "at": "2026-04-21T10:25:11.450Z",
  "session_id": "abc123",
  "detected_lang": "yue",
  "tts_locale": "yue-HK",
  "method": "particle",
  "forced": false,
  "fast_path": "weather"          // null when full path is used
}
```

`fast_path` ∈ `null | "weather" | "aqi" | "warnings" | "weather_and_air" | "chitchat"`.

### `tool_call.start` — a tool is about to run

```json
{
  "type": "tool_call.start",
  "at": "2026-04-21T10:25:12.101Z",
  "name": "transport.get_mtr_next_trains",
  "args": { "station_name": "上環" },
  "query_lang": "zh-Hant"           // translated query language
}
```

### `tool_call.result` — a tool finished

```json
{
  "type": "tool_call.result",
  "at": "2026-04-21T10:25:12.712Z",
  "name": "transport.get_mtr_next_trains",
  "status": "ok",                   // "ok" | "error" | "timeout" | "rate_limited"
  "latency_ms": 611,
  "error": null                     // string when status != "ok"
}
```

### `turn.llm_first_token` — the model has started emitting tokens

Used by the UI to hide a "drafting…" placeholder or caret.

```json
{ "type": "turn.llm_first_token", "at": "2026-04-21T10:25:13.002Z" }
```

### `turn.token` — streaming text delta

```json
{ "type": "turn.token", "at": "2026-04-21T10:25:13.020Z", "text": "上環站" }
```

Concatenate `text` values in order to reconstruct the final reply. The stream ends with `turn.final`. Robots should pass deltas straight to incremental TTS if latency matters.

### `turn.final` — full response envelope

```json
{
  "type": "turn.final",
  "at": "2026-04-21T10:25:16.880Z",
  "data": {
    "session_id": "abc123",
    "text": "<full reply>",
    "lang": {
      "source": "detected",
      "primary_lang": "yue",
      "upstream_langs_available": ["en", "zh-Hant"],
      "translation_applied": true
    },
    "citations": [
      {
        "tool": "transport.get_mtr_next_trains",
        "upstream": "rt.data.gov.hk/mtr",
        "fetched_at": "2026-04-21T10:25:12.712Z",
        "upstream_langs": ["en", "zh-Hant"],
        "translation_applied": true
      }
    ],
    "tool_trace": [
      {
        "index": 1,
        "name": "transport.get_mtr_next_trains",
        "args": { "station_name": "上環" },
        "status": "ok",
        "latency_ms": 611,
        "result_summary": "7 trains @ Sheung Wan"
      }
    ],
    "followups": [],
    "elapsed_ms": 7287
  }
}
```

`followups` carries the clarifying question when `meta.ask_user` fired.

### `error` — protocol / handler error (not a tool error)

```json
{ "type": "error", "message": "unknown type: 'foo'" }
```

---

## Client → server commands

### `turn` — submit a user message

```json
{
  "type": "turn",
  "text": "我想由上環去沙田",
  "locale_override": null          // or a BCP-47-style tag (see below)
}
```

- `text` — required, 1-4000 chars.
- `locale_override` — optional. When set to a non-`null` value, the orchestrator bypasses language detection for this turn *and* every subsequent turn in the session until it changes. Use `"auto"` (or `null`) to restore detection.

Supported `locale_override` codes in v0.1: `auto`, `yue`, `zh-Hant`, `zh-Hans`, `en`, `ja`, `ko`, `fr`, `de`, `es`, `th`, `tl`, `id`, `vi`. Other fastText-recognised tags pass through with best-effort behaviour.

### `set_locale` — change the session's forced locale

```json
{ "type": "set_locale", "locale": "yue" }
```

Server responds with `locale_set`. Persists across subsequent turns.

---

## Event ordering guarantees

For each `turn` command you will see **exactly**:

```
turn.start
( tool_call.start + tool_call.result )*   # zero or more pairs, in parallel-safe order
turn.llm_first_token                      # only when the full synth path is used
( turn.token )*                           # zero or more tokens
turn.final                                # always the last event for the turn
```

Edge cases:
- **Chitchat fast path** emits only `turn.start` + `turn.final` (no tool calls, no tokens — the reply is canned).
- **Fast-path tool intents** (weather/AQI/warnings) emit `turn.start` + tool events + `turn.llm_first_token` + tokens + `turn.final` (no intent-picking LLM hop).
- **Errors** in the tool dispatcher appear as `tool_call.result` with `status != "ok"`; the orchestrator still produces a `turn.final`. Only protocol-level framing errors use the `error` event.

---

## Minimal client example (Python)

```python
import asyncio, json
import websockets

async def chat(session_id: str, text: str) -> None:
    async with websockets.connect(f"ws://localhost:8080/ws/{session_id}") as ws:
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "ready":
                await ws.send(json.dumps({"type": "turn", "text": text}))
            elif msg["type"] == "turn.token":
                print(msg["text"], end="", flush=True)
            elif msg["type"] == "turn.final":
                print("\n---", msg["data"]["elapsed_ms"], "ms")
                return

asyncio.run(chat("robot-1", "我想由上環去沙田"))
```

## Minimal client example (browser)

```javascript
const ws = new WebSocket(`ws://${location.host}/ws/robot-1`);
let reply = "";
ws.onmessage = (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.type === "ready")       ws.send(JSON.stringify({type:"turn", text:"hi"}));
  if (msg.type === "turn.token")  reply += msg.text;
  if (msg.type === "turn.final")  console.log("final:", reply, msg.data.elapsed_ms, "ms");
};
```

---

## Integration notes for the robotics platform

- **TTS on streaming**: pipe `turn.token` straight into an incremental TTS engine; use `detected_lang` from `turn.start` to pick the voice locale. Recommended HK TTS: Azure `yue-HK-WanLungNeural` for Cantonese; `zh-HK-HiuGaaiNeural` for Mandarin-in-HK.
- **Tool trace for UI**: the tool-call events are fire-and-forget; show them optimistically. A robot can narrate ("checking the MTR now…") from `tool_call.start` and update on `tool_call.result`.
- **Cancel**: not supported in v0.1. Closing the WebSocket mid-turn is safe; tool calls will finish but their results are dropped. A `cancel` event is on the v0.2 roadmap.
- **Idle keepalive**: none. The Tailscale tailnet keeps the connection warm.
- **Session TTL**: 24 h default (see `SESSION_TTL_HOURS` in `.env`).
- **Personal-data posture**: PII redacted at ingress by default. Do not send credit-card numbers, passwords, or unredacted personal data over the protocol.
