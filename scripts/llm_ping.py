"""`just llm-ping` — prove connectivity to LM Studio on the Mac Studio.

Prints the configured base URL, whether it's reachable, which models are advertised,
and runs a Cantonese tool-call smoke test against the configured model.
"""
# ruff: noqa: RUF001  # Cantonese strings legitimately use fullwidth punctuation.

from __future__ import annotations

import asyncio
import json
import sys

from smcity.llm import LLMError, chat, ping
from smcity.settings import get_settings

TOOL_SCHEMA = {
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


async def _run() -> int:
    s = get_settings()
    print(f"base_url : {s.llm_base_url}")
    print(f"model    : {s.llm_model}")

    reachable, models = await ping()
    print(f"reachable: {reachable}")
    print(f"models   : {models or '(none)'}")

    if not reachable:
        print("\n[FAIL] cannot reach LM Studio — check Tailscale + Mac Studio.")
        return 1
    if s.llm_model not in models:
        print(f"\n[WARN] configured model {s.llm_model!r} not in advertised list")
        return 2

    print("\ncantonese tool-call smoke test ...")
    try:
        reply = await chat(
            [
                {"role": "system", "content": "When asked about weather, call the tool."},
                {"role": "user", "content": "而家天氣點呀？喺中環。"},
            ],
            tools=[TOOL_SCHEMA],
        )
    except LLMError as err:
        print(f"[FAIL] {err}")
        return 3

    print(f"elapsed_ms: {reply.elapsed_ms}")
    print(f"usage    : {reply.usage}")
    if reply.tool_calls:
        tc = reply.tool_calls[0]
        print(f"tool_call: {tc['name']} {tc['arguments']}")
        return 0
    print(f"[WARN] no tool_calls emitted (text={reply.text!r}); tool-calling path may be off.")
    print(json.dumps(reply.tool_calls, indent=2, ensure_ascii=False))
    return 4


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
