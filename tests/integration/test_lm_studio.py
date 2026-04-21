"""Integration test — requires the Mac Studio LM Studio endpoint on Tailscale.

Skipped automatically when the endpoint is unreachable, so CI off-tailnet stays green.
Run explicitly with:  uv run pytest -m integration
"""
# ruff: noqa: RUF001  # Cantonese strings legitimately use fullwidth punctuation.

from __future__ import annotations

import pytest

from smcity.llm import chat, ping
from smcity.settings import get_settings

pytestmark = pytest.mark.integration

WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get current weather for a HK location.",
        "parameters": {
            "type": "object",
            "properties": {
                "location": {"type": "string"},
                "units": {"type": "string", "enum": ["c", "f"]},
            },
            "required": ["location"],
        },
    },
}


async def test_lm_studio_advertises_configured_model(lm_studio_available: bool) -> None:
    if not lm_studio_available:
        pytest.skip("LM Studio unreachable — check Tailscale")
    reachable, models = await ping()
    assert reachable is True
    assert get_settings().llm_model in models


async def test_lm_studio_emits_toolcall_on_cantonese_weather_prompt(
    lm_studio_available: bool,
) -> None:
    if not lm_studio_available:
        pytest.skip("LM Studio unreachable")

    reply = await chat(
        [
            {"role": "system", "content": "Call the weather tool when asked about weather."},
            {"role": "user", "content": "而家天氣點呀？喺中環。"},
        ],
        tools=[WEATHER_TOOL],
    )

    assert reply.tool_calls, f"expected a tool_call, got content={reply.text!r}"
    call = reply.tool_calls[0]
    assert call["name"] == "get_weather"
    assert "{" in call["arguments"]
