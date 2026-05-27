"""OpenTelemetry / Phoenix Arize tracing for the smcity agent.

Spans are produced for:
  * **smcity.turn**    — wraps each user request from /turn or /ws. Attributes:
                         `session.id`, `user.text`, `language`, `reply.text`,
                         `duration_ms`, citations summary.
  * **llm.chat.<role>** — manually-opened parent spans around every gpt-oss-120b
                         call. `<role>` names the purpose: `decide`,
                         `synthesis`, `gate_retry`, `chain_rules_retry`,
                         `invariant_retry`, `synthesis_retry`. The auto-
                         instrumented `ChatCompletion` span nests under this,
                         so the trace reads top-to-bottom as a labelled
                         lifecycle instead of four identical ChatCompletion
                         rows. See `smcity/orchestrator.py`.
  * **tool.<name>**    — wraps each tool call inside `ToolRegistry.dispatch`.
                         Attributes: `tool.name`, `tool.args` (JSON),
                         `tool.result_summary`, `tool.status`, `tool.cached`,
                         `tool.latency_ms`, `tool.category` (poi/transport/...),
                         `tool.locale`, `tool.translation_applied`, and on
                         error: `exception.*`.
  * outbound httpx     — auto-instrumented by
                         `opentelemetry-instrumentation-httpx`. The span name
                         is rewritten from bare "GET" / "POST" to a
                         destination-named label like `hk.als.lookup`,
                         `hk.kmb.eta`, `osm.overpass`, `hko.weather` via
                         `_http_request_hook` (see below). LM Studio's own
                         `/v1/chat/completions` endpoint is excluded from
                         this instrumentation because the OpenAI SDK
                         instrumentation already records those calls as
                         `ChatCompletion` spans — recording both would
                         double-count every LLM call.

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


# --- httpx span readability ----------------------------------------------
#
# `opentelemetry-instrumentation-httpx` labels every outbound HTTP request
# with the bare method ("GET" / "POST"). In Phoenix's spans list view that
# means 145K identical-looking rows — you can't tell ALS from KMB from
# Overpass without clicking in. We install a `request_hook` that rewrites
# the span name from the URL host + path. Cheap (regex-free, host-prefix
# matching) and reversible (only the span name changes; the URL attribute
# is preserved by the instrumentation itself).
#
# The mapping is data-driven so future upstreams just need an entry.

_HTTP_SPAN_NAMES: tuple[tuple[tuple[str, str], str], ...] = (
    # (host_contains, path_prefix) → span name
    (("als.gov.hk", ""), "hk.als.lookup"),
    (("rt.data.gov.hk", "/v1/transport/mtr"), "hk.mtr.next_trains"),
    (("data.etabus.gov.hk", ""), "hk.kmb.eta"),
    (("rt.data.gov.hk", "/v2/transport/citybus"), "hk.citybus.eta"),
    (("data.etagmb.gov.hk", ""), "hk.gmb.eta"),
    (("rt.data.gov.hk", "/v2/transport/citybus/route-stop"), "hk.citybus.route_stops"),
    (("api.data.gov.hk", "/v1/historical-archive/get-file"), "hk.hko.archive"),
    (("rt.data.gov.hk", "/v1/weather"), "hk.hko.weather"),
    (("rt.data.gov.hk", "/v8/weather"), "hk.hko.weather"),
    (("overpass-api.de", ""), "osm.overpass"),
    (("nominatim.openstreetmap.org", ""), "osm.nominatim"),
    (("portal.csdi.gov.hk", ""), "hk.csdi.featureserver"),
    (("housingauthority.gov.hk", ""), "hk.hkha.estates"),
)


def _http_request_hook(span: Any, request: Any) -> None:
    """Rename outbound HTTP spans from bare "GET" / "POST" to a destination
    label like "hk.als.lookup" so the Phoenix spans list reads as a directory
    of upstreams instead of a wall of unlabeled rows. Best-effort — any
    exception is swallowed so a misshaped request never breaks instrumentation.
    """
    import contextlib

    with contextlib.suppress(Exception):
        url = request.url
        host = str(url.host or "").lower()
        path = str(url.path or "")
        for (host_match, path_prefix), name in _HTTP_SPAN_NAMES:
            if host_match in host and path.startswith(path_prefix):
                span.update_name(name)
                return
        # Unknown upstream — keep the method but include the host so spans
        # are at least groupable. Falls back gracefully when the URL has
        # no host (file:// etc).
        if host:
            span.update_name(f"http.{host}")


_LM_STUDIO_EXCLUDE_PATTERNS: tuple[str, ...] = (
    # Match anything pointed at LM Studio's chat-completions endpoint. The
    # env var is a comma-separated list of regex fragments; matching is
    # against the full URL. We list the host:port plus the path so a
    # different LM Studio (e.g. proxying a private model) is still
    # captured if the operator changes only the host.
    r":1234/v1/chat/completions",
    r":1234/v1/embeddings",
)


def _set_httpx_excluded_urls() -> None:
    """Append LM Studio's chat-completions endpoint to
    OTEL_PYTHON_HTTPX_EXCLUDED_URLS so httpx doesn't double-trace every
    LLM call (the OpenAI SDK instrumentation already records those as
    ChatCompletion spans). Preserves any patterns the operator already set.
    """
    existing = os.environ.get("OTEL_PYTHON_HTTPX_EXCLUDED_URLS", "")
    patterns = list(_LM_STUDIO_EXCLUDE_PATTERNS)
    if existing:
        patterns.insert(0, existing)
    os.environ["OTEL_PYTHON_HTTPX_EXCLUDED_URLS"] = ",".join(patterns)


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

    # Auto-instrument outbound httpx calls (data.gov.hk, OSM, ALS, etc.) —
    # with two readability tweaks for the Phoenix UI:
    #   1. Skip LM Studio's chat-completions endpoint. The OpenAI SDK
    #      instrumentation already records those calls as ChatCompletion
    #      spans; recording them as `POST` spans too produces duplicate
    #      rows with identical durations and zero added signal. The env
    #      var `OTEL_PYTHON_HTTPX_EXCLUDED_URLS` is the documented way to
    #      filter (the instrumentation reads it via `get_excluded_urls`).
    #   2. Rename the span via a request_hook so it shows up as e.g.
    #      `hk.kmb.eta` instead of bare `GET` — see `_http_request_hook`.
    _set_httpx_excluded_urls()
    try:
        HTTPXClientInstrumentor().instrument(
            tracer_provider=provider,
            request_hook=_http_request_hook,
            async_request_hook=_http_request_hook,
        )
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
