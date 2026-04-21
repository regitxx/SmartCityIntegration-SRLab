"""Orchestrator unit tests — mocks LM Studio and every tool endpoint."""
# ruff: noqa: RUF001  # CJK punctuation intentional.

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from smcity.llm import LLMReply
from smcity.orchestrator import Orchestrator
from smcity.schemas import TurnRequest
from smcity.session import SessionStore
from smcity.tools.transport import MTR_NEXT_TRAIN_URL


def _scripted_chat(responses: list[LLMReply]) -> Any:
    queue = list(responses)

    async def _fake(messages: list[dict[str, Any]], **_: Any) -> LLMReply:
        if queue:
            return queue.pop(0)
        return LLMReply(text="(no more canned replies)", tool_calls=[], usage={}, elapsed_ms=5)

    return _fake


@pytest.mark.asyncio
@respx.mock
async def test_cantonese_mtr_query_triggers_tool_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.get(MTR_NEXT_TRAIN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": 1,
                "data": {
                    "ISL-SHW": {
                        "UP": [{"dest": "CHW", "ttnt": "2", "seq": 1, "plat": "1"}],
                        "DOWN": [{"dest": "KET", "ttnt": "3", "seq": 1, "plat": "2"}],
                    }
                },
            },
        )
    )
    first = LLMReply(
        text="",
        tool_calls=[
            {
                "id": "call-1",
                "name": "transport.get_mtr_next_trains",
                "arguments": json.dumps({"station_name": "上環"}),
            }
        ],
        usage={},
        elapsed_ms=150,
    )
    second = LLMReply(text="上環站下班車大約 2 分鐘到。", tool_calls=[], usage={}, elapsed_ms=120)
    from smcity import orchestrator as orch_module

    monkeypatch.setattr(orch_module, "chat", _scripted_chat([first, second]))

    store = SessionStore(tmp_path / "sessions.sqlite3")
    orch = Orchestrator(store)
    req = TurnRequest(session_id="yue-1", text="我喺上環，下班車幾時到？")
    resp = await orch.handle_turn(req)

    assert resp.lang.primary_lang == "yue"
    assert resp.tool_trace, "expected at least one tool call"
    assert resp.tool_trace[0].name == "transport.get_mtr_next_trains"
    assert resp.tool_trace[0].status == "ok"
    assert resp.citations[0].tool == "transport.get_mtr_next_trains"
    assert resp.citations[0].translation_applied is True


@pytest.mark.asyncio
async def test_clarification_gate_via_meta_ask_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = LLMReply(
        text="",
        tool_calls=[
            {
                "id": "call-ask",
                "name": "meta.ask_user",
                "arguments": json.dumps({"question": "搭 MTR、巴士定的士？", "slot": "mode"}),
            }
        ],
        usage={},
        elapsed_ms=80,
    )
    second = LLMReply(text="(synthesised)", tool_calls=[], usage={}, elapsed_ms=40)
    from smcity import orchestrator as orch_module

    monkeypatch.setattr(orch_module, "chat", _scripted_chat([first, second]))

    store = SessionStore(tmp_path / "sessions.sqlite3")
    orch = Orchestrator(store)
    req = TurnRequest(session_id="clarify-1", text="我想由上環去沙田")
    resp = await orch.handle_turn(req)

    assert "MTR" in resp.text
    assert resp.followups and "MTR" in resp.followups[0]


@pytest.mark.asyncio
async def test_forced_locale_override_is_respected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = LLMReply(text="ready.", tool_calls=[], usage={}, elapsed_ms=20)
    from smcity import orchestrator as orch_module

    monkeypatch.setattr(orch_module, "chat", _scripted_chat([first]))

    store = SessionStore(tmp_path / "sessions.sqlite3")
    orch = Orchestrator(store)
    req = TurnRequest(session_id="force-1", text="Hello", locale_override="ja")
    resp = await orch.handle_turn(req)
    assert resp.lang.source == "forced"
    assert resp.lang.primary_lang == "jpn"
