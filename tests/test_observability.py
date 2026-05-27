"""Tests for the Phoenix observability helpers.

The functions under test live in `smcity/observability.py` and exist to
keep the Phoenix trace UI legible:

- `_http_request_hook` renames outbound httpx spans from bare "GET" / "POST"
  to a destination-named label like "hk.als.lookup".
- `_set_httpx_excluded_urls` appends LM Studio's chat-completions endpoint
  to OTEL_PYTHON_HTTPX_EXCLUDED_URLS so the OpenAI SDK instrumentation
  isn't shadowed by a duplicate httpx span.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import pytest

from smcity.observability import (
    _http_request_hook,
    _set_httpx_excluded_urls,
)

# --- _http_request_hook --------------------------------------------------


@dataclass
class _FakeURL:
    host: str
    path: str


@dataclass
class _FakeRequest:
    url: _FakeURL


class _RecordingSpan:
    def __init__(self) -> None:
        self.name = "GET"

    def update_name(self, name: str) -> None:
        self.name = name


def _rename(host: str, path: str) -> str:
    span = _RecordingSpan()
    _http_request_hook(span, _FakeRequest(_FakeURL(host=host, path=path)))
    return span.name


@pytest.mark.parametrize(
    ("host", "path", "expected"),
    [
        ("www.als.gov.hk", "/lookup", "hk.als.lookup"),
        ("rt.data.gov.hk", "/v1/transport/mtr/getSchedule.php", "hk.mtr.next_trains"),
        ("data.etabus.gov.hk", "/v1/transport/kmb/stop-eta/X", "hk.kmb.eta"),
        ("rt.data.gov.hk", "/v2/transport/citybus/eta/HKI/1/1", "hk.citybus.eta"),
        ("data.etagmb.gov.hk", "/route/HKI/1", "hk.gmb.eta"),
        ("overpass-api.de", "/api/interpreter", "osm.overpass"),
        ("nominatim.openstreetmap.org", "/search", "osm.nominatim"),
        ("portal.csdi.gov.hk", "/server/services/x/FeatureServer/0/query", "hk.csdi.featureserver"),
    ],
)
def test_known_upstreams_get_descriptive_names(host: str, path: str, expected: str) -> None:
    """Each registered upstream maps to its semantic span name."""
    assert _rename(host, path) == expected


def test_unknown_host_falls_back_to_http_host() -> None:
    """An unrecognised host gets `http.<host>` so the spans still group
    by destination even if we haven't added a specific rule for that API."""
    assert _rename("api.example.com", "/v1/anything") == "http.api.example.com"


def test_empty_host_leaves_span_name_alone() -> None:
    """If the URL has no host (file://, etc.), the hook is a no-op so the
    original `GET` / `POST` name is preserved instead of producing an ugly
    `http.` prefix with nothing after it."""
    span = _RecordingSpan()
    _http_request_hook(span, _FakeRequest(_FakeURL(host="", path="/whatever")))
    assert span.name == "GET"  # the original


def test_hook_does_not_raise_on_malformed_request() -> None:
    """Best-effort: any exception during span renaming must not bubble up
    to the instrumented request, otherwise a misshaped URL could break
    every HTTP call in the agent."""
    span = _RecordingSpan()

    class _Broken:
        @property
        def url(self) -> Any:
            raise RuntimeError("url accessor broke")

    # Should NOT raise.
    _http_request_hook(span, _Broken())
    assert span.name == "GET"  # untouched on failure


# --- _set_httpx_excluded_urls --------------------------------------------


def _restore_env_var(name: str, prior: str | None) -> None:
    if prior is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = prior


def test_excluded_urls_appends_lm_studio_when_unset() -> None:
    """First call should populate the env var with our LM Studio patterns."""
    prior = os.environ.get("OTEL_PYTHON_HTTPX_EXCLUDED_URLS")
    os.environ.pop("OTEL_PYTHON_HTTPX_EXCLUDED_URLS", None)
    try:
        _set_httpx_excluded_urls()
        value = os.environ["OTEL_PYTHON_HTTPX_EXCLUDED_URLS"]
        assert ":1234/v1/" in value
    finally:
        _restore_env_var("OTEL_PYTHON_HTTPX_EXCLUDED_URLS", prior)


def test_excluded_urls_preserves_operator_provided_patterns() -> None:
    """If the operator already set the env var, _set_httpx_excluded_urls
    must NOT clobber it — it should append our patterns onto whatever's
    there. Important for self-hosted deployments where they may also be
    excluding their own internal endpoints."""
    prior = os.environ.get("OTEL_PYTHON_HTTPX_EXCLUDED_URLS")
    os.environ["OTEL_PYTHON_HTTPX_EXCLUDED_URLS"] = "internal.example.com"
    try:
        _set_httpx_excluded_urls()
        value = os.environ["OTEL_PYTHON_HTTPX_EXCLUDED_URLS"]
        # Operator's pattern preserved first, ours appended.
        assert value.startswith("internal.example.com,")
        assert ":1234/v1/" in value
    finally:
        _restore_env_var("OTEL_PYTHON_HTTPX_EXCLUDED_URLS", prior)
