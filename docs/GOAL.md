# Smart City Integration — Goal

**Project:** HK Smart City Integration for the Lab of Social Robotics.
**Started:** 2026-04-21
**Status:** Research + planning phase.

## Vision

An agentic chat system that answers Hong Kong smart-city questions (transportation, housing, public facilities, weather/AQI context) using Hong Kong government open data at [data.gov.hk](https://data.gov.hk/en/). Every external data access is a tool call orchestrated by an LLM. Text-first; audio/STT is assumed upstream. The system will later be embedded in a social robot, so **latency, accuracy, and language fidelity are non-negotiable**.

## Primary use cases

1. **Multimodal transport planning** — "I'm in Sheung Wan, how do I get to Sha Tin?" → disambiguate (MTR / bus / minibus / taxi / walking), consider traffic, weather, AQI, typhoon/rainstorm warnings, return a grounded answer with route details.
2. **Fuzzy venue lookup** — "I want to play basketball" → ask where from + preferred mode → return nearest LCSD basketball court with opening hours + route.
3. **Housing queries** — public housing, HOS, estate directories, rent/price statistics.
4. **Contextual advisories** — weather alerts, AQI warnings, MTR/bus service disruptions injected into answers automatically when relevant.

## Non-negotiables

- **Language priority:** Cantonese (廣東話) > Mandarin (普通話, Simplified + Traditional) > English > every other language supported by data.gov.hk. The system must transparently surface which languages each data source supports, and warn the user when their chosen language isn't covered.
- **Agentic tool calling:** every data access is a tool call with strict JSON-schema-validated arguments. No hallucinated bus stops, no invented ETAs.
- **Session memory:** multi-turn dialog within a session retains slots (origin, destination, mode preference, locale) and follow-ups reuse them.
- **Fast:** conversational latency budget ≤ ~1.5 s per turn for cached intents, ≤ ~3 s for fresh tool fan-outs. Sub-second disambiguation questions.
- **Clean + integration-friendly:** clear module seams (transport clients ↔ orchestrator ↔ tool registry ↔ chat UI ↔ robotics platform). The future robotics platform must be able to consume the agent over a stable transport-agnostic interface (WebSocket / SSE / gRPC).
- **Secure:** no secrets in code, inputs validated at every tool boundary, least-privilege network exposure via Tailscale in dev, no raw LLM output routed to user-sensitive surfaces without sanitization.

## Current deployment target (dev)

- **LLM:** `openai/gpt-oss-120b` served by LM Studio on Mac Studio (`earnests-mac-studio.taila366aa.ts.net:1234`, OpenAI-compatible API), reachable over Tailscale.
- **Chat UI:** lightweight web UI with tool-call visibility (for debugging) + clean user-facing render.
- **Repo:** this directory — newly created git.
- A lab-provided platform will replace / wrap this later; design assumes it.

## Success criteria (v0)

- Answer 20 representative HK transport questions with ≥ 90% factual accuracy verified against data.gov.hk.
- Handle Cantonese input at ≥ 95% language-detection accuracy.
- Median turn latency ≤ 1.5 s, p95 ≤ 3 s on current Mac Studio.
- Full transcript-level audit log of every tool call + arguments + response.

## What lives where

- `docs/GOAL.md` — this file.
- `docs/research/` — research notes (populated by agentic research pass).
- `docs/architecture/` — architecture + design docs (populated after research).
- `docs/PLAN.md` — implementation plan (to be written after research).
- Source code folders — to be scaffolded after plan approval.
