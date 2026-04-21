# SmartCityIntegration — HK Lab of Social Robotics

Agentic chat system that answers Hong Kong smart-city questions (transport, housing, public facilities, weather / AQI context) by calling [data.gov.hk](https://data.gov.hk/en/) APIs through a strict tool registry. Designed to be embedded in a social robot — **Cantonese-first (priority, not exclusive scope), 100% language coverage from v0**, fast (p50 ≤ 1.5 s), clean seams for platform integration.

## Status

**Phase:** Phase 0 scaffold (skeleton service + UI shell + LM Studio smoke test). No real tools wired yet — that's Phase 1.

LLM backbone: `openai/gpt-oss-120b` served by LM Studio on the lab's Mac Studio (`earnests-mac-studio.taila366aa.ts.net:1234`) over Tailscale. OpenAI-compatible API; native tool-calling verified.

## Quickstart

Prereqs:
- macOS or Linux
- [`uv`](https://github.com/astral-sh/uv) (`brew install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`)
- Optional: [`just`](https://github.com/casey/just) (`brew install just`)
- Tailscale connected and on the **Earnest Design Lab** tailnet so the Mac Studio LM Studio endpoint resolves.

Install and run:

```bash
# one-time
cp .env.example .env               # adjust if needed
uv sync --extra dev                # or: just install

# smoke-test the LM Studio endpoint
uv run python -m scripts.llm_ping  # or: just llm-ping

# lint + typecheck + unit tests
uv run ruff check . && uv run ruff format --check . \
  && uv run mypy smcity tests \
  && uv run pytest -q -m "not integration"
# or: just check

# run integration tests (hits live LM Studio)
uv run pytest -q -m integration    # or: just integration

# start the service
uv run uvicorn smcity.app:app --host 127.0.0.1 --port 8080 --reload
# or: just serve

# open http://localhost:8080
```

## Layout

```
smcity/
├── __init__.py
├── app.py            FastAPI app — /health, /turn, /ws/:session_id, static UI
├── llm.py            LM Studio (OpenAI-compatible) async client
├── schemas.py        public pydantic request/response models
└── settings.py       pydantic-settings config, env-driven
scripts/
└── llm_ping.py       `just llm-ping` implementation
web/
├── index.html        archive-underground chat UI shell
├── style.css
└── app.js            vanilla-JS WebSocket client + language selector
tests/
├── test_app.py       unit tests (no network)
└── integration/
    └── test_lm_studio.py  hits live LM Studio when Tailscale is up
docs/
├── GOAL.md
├── PLAN.md
├── OPEN_QUESTIONS.md
├── architecture/{ARCHITECTURE,TOOL_CATALOG,UI_STYLE}.md
└── research/0{1..5}_*.md
```

## Where to start reading

1. [docs/GOAL.md](docs/GOAL.md) — vision, non-negotiables, success criteria
2. [docs/PLAN.md](docs/PLAN.md) — phased roadmap
3. [docs/architecture/ARCHITECTURE.md](docs/architecture/ARCHITECTURE.md) — block diagram + contracts
4. [docs/architecture/TOOL_CATALOG.md](docs/architecture/TOOL_CATALOG.md) — formal LLM tool registry
5. [docs/architecture/UI_STYLE.md](docs/architecture/UI_STYLE.md) — chat UI visual spec
6. `docs/research/` — deep-dives on data.gov.hk APIs, agentic tool-calling, multilingual stack, prior art

## Verified endpoints (as of 2026-04-21)

- `GET http://earnests-mac-studio.taila366aa.ts.net:1234/v1/models` → `openai/gpt-oss-120b`
- `POST …/v1/chat/completions` with a Cantonese user prompt and a `tools=[…]` schema returns a clean `tool_calls` array.
