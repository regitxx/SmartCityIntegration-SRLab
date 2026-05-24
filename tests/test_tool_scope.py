"""Tests for the ToolScope marker rendering.

We test the mechanism (the prefix renderer + ToolSpec field plumbing), not
each individual tool's annotation. A few smoke tests on the default
registry confirm the chosen tags actually render so future renames don't
silently drop them.
"""

from __future__ import annotations

from pydantic import BaseModel

from smcity.tools import build_default_registry
from smcity.tools.registry import ToolContext, ToolScope, ToolSpec


class _NoArgs(BaseModel):
    pass


class _NoResult(BaseModel):
    pass


async def _noop(_args: _NoArgs, _ctx: ToolContext) -> _NoResult:
    return _NoResult()


def _spec(scope: ToolScope, domain: str | None) -> ToolSpec:
    return ToolSpec(
        name="test.tool",
        description_en="A test tool.",
        args_schema=_NoArgs,
        result_schema=_NoResult,
        handler=_noop,
        scope=scope,
        domain=domain,
    )


# --- mechanism tests ------------------------------------------------------


def test_default_without_domain_renders_no_marker() -> None:
    """Bare DEFAULT (no domain) means 'no scope claim' — render plain."""
    spec = _spec(ToolScope.DEFAULT, None)
    assert spec.openai_schema()["function"]["description"] == "A test tool."


def test_default_with_domain_renders_default_marker() -> None:
    spec = _spec(ToolScope.DEFAULT, "any_mode_journey")
    desc = spec.openai_schema()["function"]["description"]
    assert desc == "[DEFAULT: any_mode_journey] A test tool."


def test_specialized_with_domain_renders_specialized_marker() -> None:
    spec = _spec(ToolScope.SPECIALIZED, "mtr_only")
    desc = spec.openai_schema()["function"]["description"]
    assert desc == "[SPECIALIZED: mtr_only] A test tool."


def test_specialized_without_domain_renders_bare_specialized() -> None:
    spec = _spec(ToolScope.SPECIALIZED, None)
    desc = spec.openai_schema()["function"]["description"]
    assert desc == "[SPECIALIZED] A test tool."


def test_fallback_renders_fallback_marker() -> None:
    spec = _spec(ToolScope.FALLBACK, None)
    desc = spec.openai_schema()["function"]["description"]
    assert desc == "[FALLBACK] A test tool."


def test_fallback_with_domain_ignores_domain() -> None:
    """[FALLBACK] is a single-state marker — domain is meaningless for it."""
    spec = _spec(ToolScope.FALLBACK, "session_reset")
    desc = spec.openai_schema()["function"]["description"]
    assert desc == "[FALLBACK] A test tool."


def test_marker_does_not_affect_function_name_or_params() -> None:
    """Only the description should change — name + params stay clean."""
    spec = _spec(ToolScope.SPECIALIZED, "mtr_only")
    schema = spec.openai_schema()
    assert schema["function"]["name"] == "test.tool"
    assert "parameters" in schema["function"]


# --- smoke tests: default registry actually wires the markers up ---------


def test_default_registry_marks_plan_journey_as_default() -> None:
    registry = build_default_registry()
    spec = registry.get("transport.plan_journey")
    assert spec.scope == ToolScope.DEFAULT
    assert spec.domain == "any_mode_journey"
    assert spec.openai_schema()["function"]["description"].startswith("[DEFAULT: any_mode_journey]")


def test_default_registry_marks_plan_simple_route_as_mtr_specialized() -> None:
    registry = build_default_registry()
    spec = registry.get("transport.plan_simple_route")
    assert spec.scope == ToolScope.SPECIALIZED
    assert spec.domain == "mtr_only"
    assert spec.openai_schema()["function"]["description"].startswith("[SPECIALIZED: mtr_only]")


def test_default_registry_marks_citybus_eta_as_specialized() -> None:
    registry = build_default_registry()
    spec = registry.get("transport.get_citybus_eta_by_route_stop")
    assert spec.scope == ToolScope.SPECIALIZED
    assert spec.domain == "citybus_only"


def test_default_registry_marks_kmb_eta_as_specialized() -> None:
    registry = build_default_registry()
    spec = registry.get("transport.get_kmb_eta_by_route_stop")
    assert spec.scope == ToolScope.SPECIALIZED
    assert spec.domain == "kmb_lwb_bus_only"


def test_default_registry_marks_ask_user_as_fallback() -> None:
    registry = build_default_registry()
    spec = registry.get("meta.ask_user")
    assert spec.scope == ToolScope.FALLBACK
    assert spec.openai_schema()["function"]["description"].startswith("[FALLBACK]")


def test_default_registry_leaves_untagged_tools_unmarked() -> None:
    """Tools we haven't tagged yet (e.g. POI find_*, weather, facility) should
    render without a marker — preserves the current behavior for un-audited
    domains until we have a reason to tag them."""
    registry = build_default_registry()
    spec = registry.get("geo.find_dentist")
    assert spec.scope == ToolScope.DEFAULT
    assert spec.domain is None
    desc = spec.openai_schema()["function"]["description"]
    assert not desc.startswith("[")
