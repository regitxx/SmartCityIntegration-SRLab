"""Tests for the pre-execution tool-call gate engine.

Engine semantics and the first registered gate (ask_user_only_first_move)
are covered here. Orchestrator integration is tested separately so the
engine stays independently verifiable.
"""

from __future__ import annotations

import json
from typing import Any

from smcity.tool_call_gates import (
    ASK_USER_ONLY_GATE,
    DEFAULT_GATES,
    GateViolation,
    ToolCallGate,
    apply_gates,
)


def _call(name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": f"call-{name}",
        "name": name,
        "arguments": json.dumps(args or {}),
    }


# --- engine semantics -----------------------------------------------------


def test_engine_returns_none_on_empty_proposal() -> None:
    """No tool calls → nothing to gate."""
    assert apply_gates([]) is None


def test_engine_returns_none_when_no_gate_matches() -> None:
    """Two-tool proposal doesn't match ask_user_only — should pass through."""
    proposed = [_call("transport.plan_journey"), _call("context.get_current_weather")]
    assert apply_gates(proposed) is None


def test_engine_runs_gates_in_order() -> None:
    """Earlier gates win when both would match."""
    fired: list[str] = []

    def _stub_check(_p: list[dict[str, Any]]) -> GateViolation | None:
        fired.append("stub")
        return GateViolation(name="stub", kind="stub", corrective_prompt="stub")

    stub = ToolCallGate(name="stub", check=_stub_check)
    violation = apply_gates(
        [_call("meta.ask_user")],
        gates=[stub, ASK_USER_ONLY_GATE],
    )
    assert violation is not None
    assert violation.name == "stub"
    assert fired == ["stub"]


def test_engine_skips_to_next_gate_when_first_returns_none() -> None:
    """A gate that doesn't match yields the floor to later gates."""

    def _never(_p: list[dict[str, Any]]) -> GateViolation | None:
        return None

    inactive = ToolCallGate(name="inactive", check=_never)
    violation = apply_gates(
        [_call("meta.ask_user")],
        gates=[inactive, ASK_USER_ONLY_GATE],
    )
    assert violation is not None
    assert violation.name == "ask_user_only_first_move"


# --- ask_user_only_first_move gate ---------------------------------------


def test_gate_fires_when_only_ask_user_proposed() -> None:
    """The documented bug: agent leads with ask_user alone."""
    violation = apply_gates([_call("meta.ask_user", {"question": "Which mode?"})])
    assert isinstance(violation, GateViolation)
    assert violation.name == "ask_user_only_first_move"
    assert violation.kind == "ask_user_only"
    # Corrective prompt names the alternative tools so the LLM knows what to try
    assert "transport.plan_journey" in violation.corrective_prompt
    assert "geo.address_lookup" in violation.corrective_prompt
    assert "geo.find_" in violation.corrective_prompt


def test_gate_silent_when_ask_user_paired_with_search_tool() -> None:
    """ask_user is fine WITH another tool — the other tool does the work."""
    proposed = [_call("transport.plan_journey"), _call("meta.ask_user")]
    assert apply_gates(proposed) is None


def test_gate_silent_when_only_search_tool_proposed() -> None:
    """A legitimate search-only proposal should always pass."""
    assert apply_gates([_call("transport.plan_journey")]) is None
    assert apply_gates([_call("geo.find_dentist")]) is None


def test_gate_silent_when_multiple_search_tools_proposed() -> None:
    """Multi-tool batch (e.g., plan + weather) should pass."""
    proposed = [
        _call("transport.plan_journey"),
        _call("context.get_current_weather"),
        _call("context.get_active_warnings"),
    ]
    assert apply_gates(proposed) is None


def test_gate_silent_when_forget_me_proposed() -> None:
    """Other meta tools are not subject to this gate."""
    assert apply_gates([_call("meta.forget_me")]) is None


# --- DEFAULT_GATES sanity ------------------------------------------------


def test_default_gates_contain_ask_user_only_gate() -> None:
    assert ASK_USER_ONLY_GATE in DEFAULT_GATES
