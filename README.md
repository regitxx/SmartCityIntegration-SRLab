# SmartCityIntegration — HK Lab of Social Robotics

Agentic chat system that answers Hong Kong smart-city questions (transport, housing, public facilities, weather / AQI context) by calling [data.gov.hk](https://data.gov.hk/en/) APIs through a strict tool registry. Designed to be embedded in a social robot — **Cantonese-first**, fast (p50 ≤ 1.5 s), clean seams for platform integration.

## Status

**Phase:** Research + planning complete. Pre-implementation. No source code yet — docs only.

Current LLM backbone: `openai/gpt-oss-120b` served by LM Studio on the lab's Mac Studio over Tailscale (OpenAI-compatible API, tool-calling verified).

## Where things live

- [docs/GOAL.md](docs/GOAL.md) — vision, non-negotiables, success criteria
- [docs/PLAN.md](docs/PLAN.md) — phased implementation plan (Phase 0 → 6)
- [docs/OPEN_QUESTIONS.md](docs/OPEN_QUESTIONS.md) — blockers awaiting decisions
- [docs/architecture/ARCHITECTURE.md](docs/architecture/ARCHITECTURE.md) — block diagram, component contracts
- [docs/architecture/TOOL_CATALOG.md](docs/architecture/TOOL_CATALOG.md) — formal LLM tool registry
- `docs/research/` — deep-dives on data.gov.hk APIs, agentic tool-calling, multilingual stack, and prior art

Start with `docs/GOAL.md` → `docs/PLAN.md`.
