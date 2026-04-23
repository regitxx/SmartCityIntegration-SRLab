"""Prompt-prefix stability — the OpenAI tool list is part of the prompt and is
forwarded verbatim to LM Studio. If its order or contents flaps across
restarts, the llama.cpp KV cache for that session is invalidated and p50
latency regresses. This test pins the surface.
"""

from __future__ import annotations

import json

from smcity.tools import build_default_registry


def test_tool_schema_order_is_stable() -> None:
    a = build_default_registry().openai_schemas()
    b = build_default_registry().openai_schemas()
    assert [s["function"]["name"] for s in a] == [s["function"]["name"] for s in b]


def test_tool_schema_order_is_alphabetical() -> None:
    schemas = build_default_registry().openai_schemas()
    names = [s["function"]["name"] for s in schemas]
    assert names == sorted(names), "tool schemas must be alphabetised for cache stability"


def test_tool_schemas_are_json_serialisable_and_stable_bytes() -> None:
    """Stronger than name-only: full schema bytes must be byte-identical
    across two independent registry builds."""
    a = json.dumps(build_default_registry().openai_schemas(), sort_keys=True, ensure_ascii=False)
    b = json.dumps(build_default_registry().openai_schemas(), sort_keys=True, ensure_ascii=False)
    assert a == b
