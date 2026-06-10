# Design — 1-hop latency pipeline

**Status:** Phase 1 implemented (2026-06-10, gated off pending A/B sweep) · **Target:** median turn ≤1.5s common path (GOAL.md), p95 ≤3s
**Baseline:** v0.8.0 calibrated sweep — median 7.1s, p95 19.2s (see [TEST_CASES.md](TEST_CASES.md))

## Problem

After the v0.8.0 POI mirror removed the Overpass-504 confound and the v0.8.1
refresh backoff stabilised the mirror, turn latency is now dominated by the two
`gpt-oss-120b` hops, not by tool I/O:

- **decide** hop — `llm.chat.decide` ([orchestrator.py](../smcity/orchestrator.py) ~L231): the LLM reads the
  user turn + tool schemas and emits tool calls.
- **synthesis** hop — `llm.chat.synthesis` (~L484): the LLM turns tool results
  into the final reply.

Tool calls themselves are cheap (mirror-served `find_poi`, `address_lookup` ~100–200 ms each).
So ≈ `7.1s − tool_time ≈ decide + synth`, two roughly-equal 120B completions.

The existing **fast path** ([orchestrator.py](../smcity/orchestrator.py) L162–225) already implements the
1-hop pattern (skip decide → dispatch tools deterministically → single synth)
— but `classifier.py` only recognises weather / aqi / warnings / chitchat,
**none of which are the hot path**.

## The lever (measured)

200-row calibration corpus (`data/synth/v0.6.0_20260526_sample200_calibration.jsonl`):

| signal | value |
|---|---:|
| POI-find share of all turns | **80%** (161/200) |
| `poi_categories.categorize()` deterministic hit on POI rows | **70%** (114/161) |
| ⇒ all turns deterministically category-classifiable **today** | **~56%** |
| `categorize()` misses (registry lexicon gaps, closeable) | 47 |

Every POI turn currently pays the decide hop only to be told "call
`address_lookup` then `find_poi`" — a decision `categorize()` + the
`chain_rules` engine already make without an LLM. Removing decide for POI
collapses the common path from **2 LLM hops → 1**.

## Existing assets (reuse, don't rebuild)

- `smcity/tools/poi_categories.py::categorize(text)` — single-source multilingual
  (en / Traditional-zh, Simplified-normalised, plural-tolerant) free-text → slug matcher.
- `smcity/chain_rules.py` — already auto-completes `address_lookup → find_poi` with no 2nd LLM hop.
- `smcity/classifier.py` + the fast-path branch — the 1-hop skeleton to extend.

## Decision (approved 2026-06-09)

**Deterministic-first; gemma deferred.** Ship the deterministic POI fast-path
with zero infra change. Ambiguous queries keep using the 120B decide hop. A
small co-resident gemma classifier (fuzzy intent+slot extraction) is revisited
only if the residual miss-rate justifies the 2nd-model cost — which conflicts
with the "120b as sole no-TTL model" ops constraint and needs its own eval.

## Plan

### Phase 0 — Measure
- Pull the `llm.chat.decide` vs `llm.chat.synthesis` span-duration split from
  Phoenix (project `smcity`) on the Studio to confirm decide ≈ half of median
  and set the real per-hop baseline. *(corpus logs have total `elapsed_ms` +
  per-tool `latency_ms` but not per-hop LLM timing.)*
- ✅ Done locally: POI share + `categorize()` hit-rate above.

### Phase 1 — Deterministic POI fast-path *(the win, no new model)* — ✅ implemented 2026-06-10
1. POI branch in `classifier.py`: `categorize_all(text)` must yield **exactly
   one** slug (multi-match = ambiguous → defer) and a location must be
   extractable → `FastPathHit(intent="poi", poi_category=…, location=…)`.
   Disqualifiers (all defer): routing shape (點樣去 / "how do I get to"),
   future qualifiers, cross-domain mixes, >120-char turns.
2. Orchestrator `_poi_fast_path_tools`: `address_lookup(location)` →
   **corroborate** (below) → `find_poi(slug, lat, lng, 800m)` (same args as
   the chain rule) → single synthesis hop. Any miss falls through to the
   full 120B path (`fast_path.defer` event).
3. The 47 lexicon misses closed in the **registry** (synonym families, never
   per-example regex). Re-measured: **159/161** POI rows categorize (was
   114/161); fast-path eligible **97/161** (~48% of all turns); **0** false
   positives on non-POI rows; slug accuracy 90/92 on eligible rows.
4. Gated behind `poi_fast_path_enabled` (default **off**) for A/B + instant
   revert; flips on only after the Phase-4 sweep shows no accuracy delta.

#### HK scoping is structural — no place lists (decision 2026-06-10)
We never enumerate foreign cities (the old 8-city denylist is gone). The
government APIs are the gazetteer:

- **Weather/AQI/warnings**: any *named* location phrase defers to the LLM —
  HKO data is territory-wide, and judging whether "Tokyo" vs "Sha Tin" is in
  scope is LLM judgement. ("HK"/香港 itself counts as no-location.)
- **POI**: the extracted phrase goes to ALS. Measured 2026-06-10: ALS
  fuzzy-matches aggressively ("Tokyo" → SUN HING BUILDING **TOKYO TOWN**,
  "London" → a Robinson Road premises), so empty-candidates is NOT a
  sufficient gate. A candidate counts only when the phrase's substantive core
  (type suffixes 站/區/"station"/"area" optional, scripts normalised) appears
  in the candidate's own name_en/name_tc/district (`_corroborated_coords`).
  Live-sampled: real places corroborate (~80%), junk extractions reliably
  defer. Residual hole: foreign names that genuinely ARE HK premises names
  (Tokyo Town) — shared with the full LLM path, which trusts ALS
  `candidates[0]` the same way in `chain_rules._poi_resolver`.
- **Location extraction** is closed-class mechanism only (markers 附近/喺/
  有冇/near/in + function-word edge-stripping); an unconfident extraction
  produces a phrase ALS won't corroborate → safe defer.

Also fixed in passing (both were invariant holes on the no-synthesis path):
thanks-chitchat replies were Cantonese for ja/ko input, and the prefix-anchored
thanks pattern swallowed real requests ("唔該，附近有冇廁所？").

### Phase 2 — Transport fast-path (long tail)
Same pattern for the clearest slot-extractable transport intents (MTR next-train,
KMB/CityBus ETA by named stop). Lower volume — after POI proves out.

### Phase 3 — Gemma fallback *(DEFERRED)*
Only if Phase 1–2 residual miss-rate justifies a co-resident small model.

### Phase 4 — Scoreboard
Re-run the calibrated 200-row sweep (pinned `FUZZER_MODEL=openai/gpt-oss-120b`,
concurrency 1, `smcity-1.` host — see ops memory). Record before/after latency +
accuracy in [TEST_CASES.md](TEST_CASES.md) and make it the primary scoreboard.

## Guardrails

- **Accuracy must not regress.** Anything ambiguous (no category, multiple
  categories, non-HK / future qualifiers, unextractable location) defers to the
  full 120B path. The fast path is opt-in per-turn, binary-confident.
- Removing decide for POI must still satisfy the synthesis invariants
  (`synthesis_invariants.py`) and language fidelity — the synth hop is unchanged.
- A/B setting lets us measure accuracy delta on the same corpus before defaulting on.

## Open questions

- Location-phrase extraction robustness across 4 languages — does `address_lookup`
  already tolerate free-text location fragments, or is a light extractor needed?
- Decide/synth split (Phase 0 Phoenix pull) — if synth alone is >1.5s, Phase 1
  gets us most of the way but hitting ≤1.5s may also need synth-prompt/token trimming.
