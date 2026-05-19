# smcity agent — production container for the Mac Studio deploy.
# Two-stage build: deps in /opt/venv, run from the repo layout so the
# code's `Path(__file__).parent.parent.parent / "data"` lookups (MTR
# topology, station catalog, HKHA TC-name overlay) still work.

# --- builder ------------------------------------------------------------
FROM python:3.12-slim AS builder

# uv is our package manager — same as local dev, single-binary install.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /build

# Cache deps in their own layer — only invalidated when pyproject/uv.lock change.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# --- runtime ------------------------------------------------------------
FROM python:3.12-slim

# Non-root user.
RUN useradd --create-home --shell /usr/sbin/nologin --uid 10001 smcity

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Pull the dep venv from the builder.
COPY --from=builder --chown=smcity:smcity /opt/venv /opt/venv

# Copy the project source mirroring the repo layout — this lets the
# code's relative path lookups (smcity/tools/foo.py reads ../../data/*)
# resolve correctly without re-rooting anything.
WORKDIR /app
COPY --chown=smcity:smcity smcity ./smcity
COPY --chown=smcity:smcity smcity_fuzz ./smcity_fuzz
COPY --chown=smcity:smcity data ./data
COPY --chown=smcity:smcity web ./web

# /app/state holds the session DB (sessions.sqlite3). In production this
# directory is overlaid by a named volume so sessions persist across
# container restarts. Pre-create it in the image so the agent doesn't
# have to mkdir at startup if the volume mount happens to be missing.
RUN mkdir -p /app/state && chown smcity:smcity /app/state

USER smcity

# Session DB lives at /app/state/sessions.sqlite3 — owned by smcity user.
# To persist across container restarts, mount a named volume on /app/state.
# (As of v0.4.16 — pre-v0.4.16 deploys mounted the volume at /app/data
# which masked the baked-in JSON catalogs.)

EXPOSE 8080

# Sensible runtime defaults. Override LLM_BASE_URL / LLM_MODEL in compose.
ENV BIND_HOST=0.0.0.0 \
    BIND_PORT=8080 \
    LLM_BASE_URL=http://host.docker.internal:1234/v1 \
    LLM_MODEL=openai/gpt-oss-120b \
    LLM_TIMEOUT_S=120 \
    LOG_LEVEL=INFO

# Health probe — docker + the Tailscale sidecar's depends_on uses this.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; \
                 r=urllib.request.urlopen('http://localhost:8080/health', timeout=5); \
                 sys.exit(0 if r.status==200 else 1)" || exit 1

CMD ["uvicorn", "smcity.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
