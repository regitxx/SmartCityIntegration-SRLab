"""Unit tests for the FastAPI app — no network required."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from smcity import app as app_module
from smcity import llm as llm_module
from smcity.app import app
from smcity.llm import LLMReply


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_ping() -> tuple[bool, list[str]]:
        return True, ["openai/gpt-oss-120b"]

    async def fake_chat(messages: list[dict[str, Any]], **_: Any) -> LLMReply:
        user_text = next((m["content"] for m in messages if m["role"] == "user"), "")
        return LLMReply(
            text=f"echo: {user_text}",
            tool_calls=[],
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            elapsed_ms=12,
        )

    monkeypatch.setattr(app_module, "ping", fake_ping)
    monkeypatch.setattr(app_module, "chat", fake_chat)
    monkeypatch.setattr(llm_module, "ping", fake_ping)
    monkeypatch.setattr(llm_module, "chat", fake_chat)


def test_health_ok() -> None:
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["llm_reachable"] is True
    assert body["llm_model"] == "openai/gpt-oss-120b"


def test_turn_echo_auto_locale() -> None:
    with TestClient(app) as client:
        r = client.post(
            "/turn",
            json={"session_id": "t1", "text": "hello"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == "t1"
    assert body["text"].startswith("echo:")
    assert body["lang"]["source"] == "detected"
    assert body["lang"]["primary_lang"] == "auto"
    assert body["tool_trace"] == []


def test_turn_forced_locale() -> None:
    with TestClient(app) as client:
        r = client.post(
            "/turn",
            json={"session_id": "t2", "text": "你好", "locale_override": "yue"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["lang"]["source"] == "forced"
    assert body["lang"]["primary_lang"] == "yue"


def test_turn_rejects_empty_text() -> None:
    with TestClient(app) as client:
        r = client.post("/turn", json={"session_id": "t3", "text": ""})
    assert r.status_code == 422


def test_websocket_ready_and_set_locale() -> None:
    with TestClient(app) as client, client.websocket_connect("/ws/session-abc") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "ready"
        assert hello["session_id"] == "session-abc"
        ws.send_json({"type": "set_locale", "locale": "yue"})
        ack = ws.receive_json()
        assert ack == {"type": "locale_set", "locale": "yue", "at": ack["at"]}


def test_websocket_turn_roundtrip() -> None:
    with TestClient(app) as client, client.websocket_connect("/ws/s2") as ws:
        ws.receive_json()  # ready
        ws.send_json({"type": "turn", "text": "how to Sha Tin?"})
        start = ws.receive_json()
        assert start["type"] == "turn.start"
        final = ws.receive_json()
        assert final["type"] == "turn.final"
        data = final["data"]
        assert data["session_id"] == "s2"
        assert data["text"].startswith("echo:")
