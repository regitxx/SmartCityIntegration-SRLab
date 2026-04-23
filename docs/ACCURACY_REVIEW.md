# Response-accuracy risk register

**Purpose:** this file enumerates every known way the agent's responses can be wrong, grouped by the code layer that's responsible, with the specific `failure_reasons` tag the fuzz judge should emit when it catches each one. It is the ground-truth reference for what accuracy means in this system.

Two audiences:
- **You**, when you run `smcity_fuzz run` + `export` and read failure rows — so you can recognise which failures are real and which are rubric noise.
- **Claude / Gemini**, when you paste the export into a new session and ask "diagnose and patch" — the file points at the specific code layer each failure class lives in.

This is a static document. **The fuzzer is how you actually measure accuracy.** This is the map; the fuzzer is the territory.

---

## 1. Intent misidentification (`failure_reasons: wrong_tool`)

**Layer:** `smcity/prompts.py` SYSTEM_PROMPT + `smcity/classifier.py` fast-path + the LLM's tool-selection step.

| Failure mode | Past live incident | What the judge should catch |
|---|---|---|
| Agent asked "MTR or bus?" before calling `plan_journey` for a free-form "how do I get from X to Y" query | v0.1.1 live bug | Tool trace shows `meta.ask_user` when tool trace SHOULD show `transport.plan_journey` or `plan_simple_route` |
| Agent called MTR tool when user said "walk" / "行路" / "步行" | Pre-v0.2 live bug | `plan_simple_route` in trace when user requested walking |
| Agent called KMB ETA when user asked about Citybus (operator confusion) | potential | Operator in the reply doesn't match the operator in the tool name |
| Agent searched OSM POIs for something the registry has a dedicated tool for (e.g. MTR entrances via OSM instead of `get_mtr_next_trains`) | potential | The LLM went for `geo.search_osm_pois` when a narrower tool fit better |
| Fast-path classifier fired for a non-trivial query (false chitchat) | potential | `fast_path` in `turn.start` event is set, but the user wrote a substantive question |
| Fast-path classifier missed an obvious weather question | potential | `fast_path` is null, yet the only tool called was `context.get_current_weather` (classifier should have caught it) |

**Mitigation knob:** `SYSTEM_PROMPT` keyword-to-tool table in `smcity/prompts.py` lines 28–60.

---

## 2. Language drift (`wrong_language`, `english_in_cantonese`, `mandarin_in_cantonese`)

**Layer:** `smcity/langrouter/detect.py` + `smcity/prompts.py` `language_stick_reminder` + `smcity/cantonese_polish.py`.

| Failure mode | Past live incident | What the judge should catch |
|---|---|---|
| Reply in English when user wrote Cantonese | common | Primary-language reply mismatch — any English prose in a yue turn |
| Reply in Mandarin when user wrote Cantonese (formal `的/是/在/了` instead of `嘅/係/喺/咗`) | risk | `mandarin_in_cantonese` — text passes script check (繁體) but is Mandarin register; polish missed it |
| Tool output's bilingual fields (`name_en`, `name_tc`, `name_sc`) "pulled" the reply into the wrong language | documented risk | Reply language mismatches `detected_lang`; tool_trace contains bilingual fields |
| Partial drift mid-sentence ("依家香港係 27 度, humidity is 76 %") | risk | Language flip inside one sentence |
| Simplified Chinese returned to a Traditional-Chinese user | potential | OpenCC `s2hk` should have caught this |
| Polish over-applied — e.g. Mandarin word that happens to be the same in Cantonese gets wrongly substituted | risk | Substitution made the text less natural, not more |

**Mitigation knobs:**
- `cantonese_style_block()` few-shot exemplars in `smcity/prompts.py`.
- `language_stick_reminder()` injected after tool calls.
- `smcity/cantonese_polish.py` character-level subs with regex guards (`的(?!士)`, `在(?![於座下])`, etc.).

---

## 3. Factual drift vs tool output (`hallucinated_fact`, `stale_data`)

**Layer:** the LLM itself after `_stream_final` — the synthesis step. Nothing in code can prevent this; only the prompt and post-polish discipline can.

| Failure mode | Past live incident | What the judge should catch |
|---|---|---|
| Agent cited pool A but the tool returned pool B | risk | Cross-reference names in reply vs names in tool_trace result summary |
| Agent invented a court / station / estate name | risk | Name in reply appears in NEITHER the tool_trace results NOR the bundled reference data |
| Agent gave the wrong temperature (e.g. 25 °C when HKO said 27 °C) | risk | Numeric mismatch between reply and tool result |
| Agent claimed "no warnings active" while `warnsum` tool_trace had active warnings | risk | Boolean mismatch between reply claim and tool data |
| Agent quoted an ETA that was newer/older than the KMB API returned | risk | Time value mismatch |
| Agent invented a bus route number | risk | Route number in reply not present in any ETA tool result |
| Agent quoted stale bundled data as if it were live (e.g. "15 courts in HK" when we show 305 live) | impossible after v0.3.4 | covered by migration, but keep judge alert |

**Mitigation knob:** `SYSTEM_PROMPT` line "Every factual claim about HK city state comes from a tool call. Do not invent MTR stations, bus routes, weather numbers, AQHI bands, or addresses."

---

## 4. Wrongful refusals (`refused_wrongly`)

**Layer:** `SYSTEM_PROMPT` safety rules + `meta.ask_user` tool.

| Failure mode | Past live incident | What the judge should catch |
|---|---|---|
| Agent refused a legitimate transport query ("I can't plan routes") when it absolutely can | risk | Refusal text like "I don't have access to" for a question the registry clearly supports |
| Agent redirected to the HKHA portal for a non-personal housing question ("how many blocks in Choi Hung Estate?") | risk | HKHA-portal URL appearing when the question is just factual |
| Agent declined to answer in a language it supports | risk | "I can only respond in English" in a non-English reply |
| Agent refused weather query because of the no-medical-advice rule misfiring on "is it safe for my asthma to go outside?" | potential | Over-conservative refusal on a legitimate AQHI-relevant question |

**Mitigation knob:** the ONE deliberate refusal rule is in `housing.get_estate_info`'s tool description: personal application / waiting-list status ⇒ redirect only. Everything else should answer.

---

## 5. Structural leaks (`harmony_leak`, `empty_reply`, `incomplete`)

**Layer:** `smcity/llm.py` `extract_harmony_tool_calls` + `smcity/orchestrator.py` `_stream_final` retry + `smcity/orchestrator.py` `_rewrite_source_footer`.

| Failure mode | Past live incident | What the judge should catch |
|---|---|---|
| Reply contains `<\|start\|>assistant<\|channel\|>commentary to=functions.X <\|message\|>{…}` tokens | v0.1.1 live bug | Any `<\|` substring in reply text |
| Reply contains bare-leak `tool_name json{…}` | v0.1.3 live bug | Reply contains a registered tool name followed by a JSON object |
| Reply is the empty string / only whitespace | v0.2 walking-only collapse | Reply length < 5 chars while tool_trace had successful tools |
| Reply truncated mid-sentence | potential | Reply doesn't end in punctuation or a full-width period |
| Reply contains an LLM-invented `src: fake_tool` line not matching the real citations | v0.1.3 live bug | `src:` line in reply doesn't match `citations[]` in the final response |
| Reply contains raw JSON from a tool result that wasn't summarised | potential | `{` / `"` literal in reply that doesn't belong to natural prose |

**Mitigation knobs:** three layers of defence already shipped:
- Streaming swallows tokens containing `<|` (see `smcity/llm.py` `chat_stream`).
- `extract_harmony_tool_calls` recovers leaked tool calls from both canonical and bare formats.
- `_rewrite_source_footer` strips any `src:` line the LLM wrote and appends one built deterministically from `citations[]`.

If any of these fire on a real turn, the fuzzer should catch it with one of the tags above.

---

## 6. Disambiguation failure (`incomplete`, `wrong_tool`)

**Layer:** `SYSTEM_PROMPT` disambiguation rules + `meta.ask_user`.

| Failure mode | Past live incident | What the judge should catch |
|---|---|---|
| Ambiguous origin ("from work") answered without asking | risk | tool_trace has `plan_*` but the user never named an origin |
| Ambiguous mode fired `meta.ask_user` when `plan_journey` would have served (the prompt was updated to prefer plan_journey in v0.2) | pre-v0.2 bug | tool_trace shows `meta.ask_user` for mode; SYSTEM_PROMPT says DO NOT |
| Agent asked two questions in one turn ("which bus AND where from?") | risk | `meta.ask_user` content contains both "and" + a second question mark |
| Agent failed to resolve "nearest X" because user_location was missing but it plowed ahead | risk | Tool was called with lat=0, lng=0 or a hallucinated coord |

---

## 7. Routing correctness (`wrong_tool`, `hallucinated_fact`)

**Layer:** the Dijkstra MTR planner + OTP2 client.

| Failure mode | Past live incident | What the judge should catch |
|---|---|---|
| Simple-route planner returned a route that passes through a non-existent interchange | covered by test_transport_planner.py | Interchange stations in the leg list must be on both the from-line and the to-line |
| Dead-end walk from origin to MTR because nearest station > `_MAX_WALK_TO_MTR_M` (1500 m), but agent claimed "walk 5 min to station" anyway | risk | Agent-stated walk duration doesn't match tool's returned `walk_from_origin_m` |
| OTP2 sidecar returned bounds-error but agent carried on with stale cached data | covered by test_otp2.py | Tool status = "error", but reply contains an itinerary |
| Taxi fare estimate missed the $2 peak surcharge / cross-harbour toll (we don't model those) | known limitation | Agent claims "total fare is exactly $X" rather than "approximately $X plus surcharges" |

---

## 8. Rate-limit + session hygiene (`tool_error`, `incomplete`)

**Layer:** `smcity/ratelimit.py` + WebSocket / `/turn` entrypoints.

| Failure mode | What the judge should catch |
|---|---|
| Agent replied on a rate-limited turn (rate limit should have rejected) | retry_after in structured error response; still serving a reply |
| Cross-turn session bleed (fuzzer uses unique session_ids per turn to avoid this — but manual use doesn't) | Agent's reply references a previous turn's context that shouldn't exist |

---

## 9. Things the judge should NOT flag

To avoid rubric noise, the judge should ignore:

- **Polite HK particles** (呀/啦/喎/嘞/咋) in Cantonese replies — these are correct.
- **English tool names in the `src:` footer** — the footer is deterministic and added by the service.
- **Cold-fetch latency > 2 s** for facility / housing / CSDI tools — their module caches are per-process; first call pays the fetch cost.
- **Terse fast-path chitchat replies** — they are allowed to be one-liners.
- **Code-switch** in the user's own question (not the reply) — many HK users mix languages.

---

## 10. How to run a full accuracy pass

```bash
# 1. One-off prereq: load gpt-oss-20b in LM Studio alongside 120b (same instance).
#
# 2. Run a 40-turn campaign across all 5 personas × 22 topics × 4 languages
#    (full matrix is 440 cells; 40 turns is a quick first pass):
uv run python -m smcity_fuzz run --mode ws --turns 40 --concurrency 2

# 3. Export the handoff Markdown for a frontier LLM to diagnose:
uv run python -m smcity_fuzz export --out handoff/$(date +%Y-%m-%d).md --max-failures 30

# 4. Paste the markdown into a Claude / Gemini chat and ask:
#    "diagnose the top 10 failures against ACCURACY_REVIEW.md and propose
#    minimal, targeted code patches."
```

The exported Markdown banner already tells the receiving LLM "this is diagnostic evidence, not a fix plan — a separate engineer decides patches." This doc is the criteria it grades against.
