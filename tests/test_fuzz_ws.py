# ruff: noqa: RUF001
"""Tests for smcity_fuzz.ws_transport + ws-mode runner.

We don't spin up a real WebSocket server — instead we inject a fake
`connect` context manager that mimics the protocol the smcity agent
emits (ready / turn.start / turn.token / turn.final).
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from smcity_fuzz.datasets import TOPICS
from smcity_fuzz.datasets import by_id as topic_by_id
from smcity_fuzz.personas import by_id as persona_by_id
from smcity_fuzz.runner import run_campaign
from smcity_fuzz.settings import FuzzSettings
from smcity_fuzz.store import read_rows
from smcity_fuzz.ws_transport import (
    WsTransportError,
    _ws_url,
    drive_turn_via_ws,
)

# --- fakes ---------------------------------------------------------------


class _FakeWebSocket:
    """Minimal in-memory stand-in for a websockets client connection."""

    def __init__(self, *, scripted_events: list[dict[str, Any]]) -> None:
        self.sent: list[str] = []
        # Events the server will push. We always prepend a 'ready' frame
        # so `drive_turn_via_ws` can drain it before sending the turn.
        self._queue = [
            {"type": "ready", "session_id": "fake", "model": "m", "version": "v", "ts": "now"},
            *scripted_events,
        ]
        self._idx = 0
        # Inter-frame delay, so ttft_ms calculation is > 0 and observable.
        self._delay_s = 0.001

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def recv(self) -> str:
        if self._idx >= len(self._queue):
            raise TimeoutError("fake ws ran out of frames")
        msg = self._queue[self._idx]
        self._idx += 1
        await asyncio.sleep(self._delay_s)
        return json.dumps(msg)


def _fake_connect_factory(scripted_events: list[dict[str, Any]]) -> Any:
    """Return an async-context-manager that yields a scripted FakeWebSocket."""

    @asynccontextmanager
    async def _connect(_url: str) -> Any:  # signature compat with websockets.connect
        yield _FakeWebSocket(scripted_events=scripted_events)

    return _connect


_TOPIC = TOPICS[0]


@pytest.fixture
def tmp_settings(tmp_path: Path) -> FuzzSettings:
    return FuzzSettings(
        base_url="http://fuzz-llm.test/v1",
        model="openai/gpt-oss-20b",
        timeout_s=5.0,
        agent_url="http://agent.test",
        agent_timeout_s=5.0,
        runs_path=str(tmp_path / "ws_runs.jsonl"),
        concurrency=1,
    )


# --- unit tests on drive_turn_via_ws -------------------------------------


def test_ws_url_upgrades_scheme() -> None:
    assert _ws_url("http://agent.test:8080", "abc") == "ws://agent.test:8080/ws/abc"
    assert _ws_url("https://agent.test", "x") == "wss://agent.test/ws/x"
    assert _ws_url("http://127.0.0.1:8080/", "s") == "ws://127.0.0.1:8080/ws/s"


@pytest.mark.asyncio
async def test_ws_transport_captures_ttft_and_tokens(tmp_settings: FuzzSettings) -> None:
    scripted: list[dict[str, Any]] = [
        {"type": "turn.start", "detected_lang": "en"},
        {"type": "tool_call.start", "name": "transport.get_mtr_next_trains", "args": {}},
        {
            "type": "tool_call.result",
            "name": "transport.get_mtr_next_trains",
            "status": "ok",
            "latency_ms": 200,
        },
        {"type": "turn.llm_first_token"},
        {"type": "turn.token", "text": "Next "},
        {"type": "turn.token", "text": "train "},
        {"type": "turn.token", "text": "3 min."},
        {
            "type": "turn.final",
            "data": {
                "session_id": "s",
                "text": "Next train 3 min.",
                "lang": {
                    "source": "detected",
                    "primary_lang": "en",
                    "upstream_langs_available": [],
                    "translation_applied": False,
                },
                "citations": [],
                "tool_trace": [
                    {
                        "index": 1,
                        "name": "transport.get_mtr_next_trains",
                        "args": {},
                        "status": "ok",
                        "latency_ms": 200,
                    }
                ],
                "followups": [],
                "elapsed_ms": 500,
            },
        },
    ]
    result = await drive_turn_via_ws(
        "next train?",
        "sess",
        settings=tmp_settings,
        connect=_fake_connect_factory(scripted),
    )
    assert result.reply == "Next train 3 min."
    assert result.token_count == 3
    assert result.ttft_ms is not None and result.ttft_ms >= 0
    assert result.elapsed_ms == 500
    assert len(result.tool_trace) == 1


@pytest.mark.asyncio
async def test_ws_transport_fast_path_without_tokens_falls_back_to_final_text(
    tmp_settings: FuzzSettings,
) -> None:
    scripted: list[dict[str, Any]] = [
        {"type": "turn.start", "detected_lang": "yue", "fast_path": "chitchat"},
        {
            "type": "turn.final",
            "data": {
                "session_id": "s",
                "text": "您好！",
                "lang": {
                    "source": "detected",
                    "primary_lang": "yue",
                    "upstream_langs_available": [],
                    "translation_applied": False,
                },
                "citations": [],
                "tool_trace": [],
                "followups": [],
                "elapsed_ms": 40,
            },
        },
    ]
    result = await drive_turn_via_ws(
        "hi", "sess", settings=tmp_settings, connect=_fake_connect_factory(scripted)
    )
    assert result.token_count == 0
    assert result.ttft_ms is None  # no turn.token → no TTFT measurement
    assert result.reply == "您好！"


@pytest.mark.asyncio
async def test_ws_transport_raises_on_error_frame(tmp_settings: FuzzSettings) -> None:
    scripted: list[dict[str, Any]] = [
        {"type": "error", "message": "rate limit exceeded; retry in 2.0s"}
    ]
    with pytest.raises(WsTransportError, match="rate limit"):
        await drive_turn_via_ws(
            "q", "sess", settings=tmp_settings, connect=_fake_connect_factory(scripted)
        )


# --- runner integration in ws mode ---------------------------------------


def _chat_completion_body(text: str) -> dict[str, Any]:
    return {"id": "c", "choices": [{"message": {"content": text}}]}


@pytest.mark.asyncio
@respx.mock
async def test_runner_ws_mode_populates_ttft_and_token_count(tmp_settings: FuzzSettings) -> None:
    respx.post("http://fuzz-llm.test/v1/chat/completions").mock(
        side_effect=[
            # synth call
            httpx.Response(200, json=_chat_completion_body("where's Central MTR?")),
            # judge call
            httpx.Response(
                200,
                json=_chat_completion_body(
                    json.dumps(
                        {
                            "intent_match": 2,
                            "language_ok": 2,
                            "tool_choice_ok": 2,
                            "factual_vs_trace": 2,
                            "coherence": 2,
                            "failure_reasons": [],
                            "summary": "ok",
                        }
                    )
                ),
            ),
        ]
    )
    scripted: list[dict[str, Any]] = [
        {"type": "turn.start", "detected_lang": "en"},
        {"type": "turn.token", "text": "At "},
        {"type": "turn.token", "text": "Central MTR."},
        {
            "type": "turn.final",
            "data": {
                "session_id": "s",
                "text": "At Central MTR.",
                "lang": {
                    "source": "detected",
                    "primary_lang": "en",
                    "upstream_langs_available": [],
                    "translation_applied": False,
                },
                "citations": [],
                "tool_trace": [],
                "followups": [],
                "elapsed_ms": 300,
            },
        },
    ]
    _, rows = await run_campaign(
        personas=(persona_by_id("english_tourist"),),
        topics=(topic_by_id("address_lookup"),),
        languages=("en",),
        settings=tmp_settings,
        mode="ws",
        ws_connect=_fake_connect_factory(scripted),
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.transport == "ws"
    assert row.token_count == 2
    assert row.ttft_ms is not None and row.ttft_ms >= 0
    assert row.reply == "At Central MTR."
    # Row persisted with the new schema fields.
    persisted = read_rows(settings=tmp_settings)
    assert persisted[0].transport == "ws"
    assert persisted[0].token_count == 2
