set shell := ["bash", "-cu"]
set dotenv-load

default:
    @just --list

# Install deps (Python 3.12 managed by uv)
install:
    uv sync --extra dev

# Start the dev server
serve:
    uv run uvicorn smcity.app:app --host "${BIND_HOST:-127.0.0.1}" --port "${BIND_PORT:-8080}" --reload

# Ping LM Studio and list available models
llm-ping:
    uv run python -m scripts.llm_ping

# Lint + typecheck + unit tests (no network)
check:
    uv run ruff check .
    uv run ruff format --check .
    uv run mypy smcity tests
    uv run pytest -q -m "not integration"

# Auto-format
fmt:
    uv run ruff format .
    uv run ruff check --fix .

# Run integration tests (hits live LM Studio — needs Tailscale)
integration:
    uv run pytest -q -m integration

# Full suite (unit + integration)
test:
    uv run pytest -q

# Wipe caches
clean:
    rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov dist build
    find . -name "__pycache__" -type d -prune -exec rm -rf {} +
