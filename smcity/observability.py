"""OpenTelemetry / Phoenix Arize tracing for the smcity agent.

Spans are produced for:
  * **turn**           — wraps each user request from /turn or /ws. Attributes:
                         `session.id`, `user.text`, `language`, `reply.text`,
                         `duration_ms`, citations summary.
  * **tool.dispatch**  — wraps each tool call inside `ToolRegistry.dispatch`.
                         Attributes: `tool.name`, `tool.args` (JSON),
                         `tool.result_summary`, `tool.status`, `tool.cached`,
                         `tool.latency_ms`, and on error: `exception.*`.
  * **llm.chat**       — auto-instrumented by `openinference-instrumentation-
                         openai`. Records model, prompt messages, response,
                         token counts, finish reason. Hooks into the
                         OpenAI-compatible client we use for LM Studio.
  * outbound httpx     — auto-instrumented by
                         `opentelemetry-instrumentation-httpx`. Records every
                         HKSAR / OSM / etc. API call as a span with method,
                         URL, status, duration.

The OTLP exporter is enabled only when ``PHOENIX_COLLECTOR_ENDPOINT`` is set
in the environment. Without it (local dev / first-boot before the operator
pastes the API key) tracing is initialised but spans go to a no-op exporter,
so the rest of the agent stack works unchanged.

Configuration env vars (read once at startup):
  PHOENIX_COLLECTOR_ENDPOINT   e.g. https://phoenix.sustainer.ai
                               (we append /v1/traces ourselves)
  PHOENIX_API_KEY              Phoenix project API key, sent as
                               `api_key=<key>` in the auth header
  PHOENIX_PROJECT_NAME         Phoenix project name (default: "smcity")
  PHOENIX_DISABLE              Set to "1" to disable tracing entirely
"""

from __future__ import annotations

import logging
import os
from typing import Any

import structlog
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_log = structlog.get_logger(__name__)

# Quiet OTEL's own logging — it's verbose under normal operation.
logging.getLogger("opentelemetry.exporter.otlp.proto.http.trace_exporter").setLevel(
    logging.WARNING
)

_DEFAULT_PROJECT = "smcity"
_PHOENIX_TRACES_PATH = "/v1/traces"
# OpenInference / Phoenix uses this resource attribute to bucket spans into
# named projects in the UI. The well-known key.
_PROJECT_NAME_ATTR = "openinference.project.name"

_provider: TracerProvider | None = None


def init_tracing(*, service_name: str = "smcity-agent", version: str | None = None) -> None:
    """Initialise the global tracer provider.

    Idempotent — calling twice does nothing. The first call:
      1. Builds a ``TracerProvider`` with project-tagged resource attributes.
      2. Adds an OTLP HTTP exporter pointed at Phoenix IF the env is set.
      3. Auto-instruments the openai SDK and httpx clients.

    Safe to call even when no Phoenix is configured — the provider is still
    set up so tests / dev runs can read spans via a NoOp / in-memory exporter.
    """
    global _provider  # noqa: PLW0603  — module-level singleton is intentional.
    if _provider is not None:
        return

    if os.getenv("PHOENIX_DISABLE") == "1":
        _log.info("tracing.disabled", reason="PHOENIX_DISABLE=1")
        return

    endpoint = (os.getenv("PHOENIX_COLLECTOR_ENDPOINT") or "").rstrip("/")
    api_key = os.getenv("PHOENIX_API_KEY") or ""
    project = os.getenv("PHOENIX_PROJECT_NAME") or _DEFAULT_PROJECT

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": version or "unknown",
            _PROJECT_NAME_ATTR: project,
        }
    )
    provider = TracerProvider(resource=resource)

    if endpoint:
        headers: dict[str, str] = {}
        if api_key:
            # Phoenix accepts the bare `api_key=...` form for arize-cloud-style
            # auth and an `Authorization: Bearer <key>` for self-hosted. We
            # send both — collectors ignore unknown headers.
            headers["api_key"] = api_key
            headers["authorization"] = f"Bearer {api_key}"
        exporter = OTLPSpanExporter(
            endpoint=f"{endpoint}{_PHOENIX_TRACES_PATH}",
            headers=headers or None,
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
        _log.info(
            "tracing.enabled",
            endpoint=endpoint,
            project=project,
            auth="api_key" if api_key else "none",
        )
    else:
        _log.info(
            "tracing.no_exporter",
            reason="PHOENIX_COLLECTOR_ENDPOINT not set",
            note="spans still produced; no remote sink",
        )

    trace.set_tracer_provider(provider)
    _provider = provider

    # Auto-instrument the OpenAI client used for LM Studio.
    try:
        from openinference.instrumentation.openai import OpenAIInstrumentor

        OpenAIInstrumentor().instrument(tracer_provider=provider)
    except Exception as err:  # pragma: no cover — instrumentation is best-effort
        _log.warning("tracing.openai_instrument_failed", error=str(err))

    # Auto-instrument outbound httpx calls (data.gov.hk, OSM, ALS, etc.).
    try:
        HTTPXClientInstrumentor().instrument(tracer_provider=provider)
    except Exception as err:  # pragma: no cover
        _log.warning("tracing.httpx_instrument_failed", error=str(err))


def shutdown_tracing() -> None:
    """Flush + shut down the provider. Call once on graceful shutdown."""
    global _provider  # noqa: PLW0603  — module-level singleton is intentional.
    if _provider is None:
        return
    try:
        _provider.shutdown()
    except Exception as err:  # pragma: no cover
        _log.warning("tracing.shutdown_failed", error=str(err))
    _provider = None


def get_tracer(name: str = "smcity") -> trace.Tracer:
    """Return a Tracer. Safe to call before init_tracing — falls back to the
    OTel global no-op provider until init_tracing wires up the real one.
    """
    return trace.get_tracer(name)


def set_attr_safe(span: trace.Span, key: str, value: Any) -> None:
    """``span.set_attribute`` that silently ignores unsupported types (lists of
    dicts, None, etc.) — OTel raises on those and we don't want a noisy span
    setter to kill a real request.
    """
    if value is None:
        return
    import contextlib

    with contextlib.suppress(Exception):
        if isinstance(value, str | bool | int | float):
            span.set_attribute(key, value)
        elif isinstance(value, list | tuple) and all(
            isinstance(v, str | bool | int | float) for v in value
        ):
            span.set_attribute(key, list(value))
        else:
            span.set_attribute(key, str(value)[:8192])


__all__ = [
    "get_tracer",
    "init_tracing",
    "set_attr_safe",
    "shutdown_tracing",
]
