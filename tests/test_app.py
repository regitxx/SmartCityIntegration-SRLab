"""FastAPI app tests — mock LM Studio at the orchestrator seam."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from smcity import app as app_module
from smcity.app import app
from smcity.llm import LLMReply


def _fake_ping() -> Any:
    async def _p() -> tuple[bool, list[str]]:
        return True, ["openai/gpt-oss-120b"]

    return _p


def _scripted_chat(replies: list[LLMReply]) -> Any:
    queue = list(replies)

    async def _fake(messages: list[dict[str, Any]], **_: Any) -> LLMReply:
        if queue:
            return queue.pop(0)
        return LLMReply(text="(out of canned replies)", tool_calls=[], usage={}, elapsed_ms=5)

    return _fake


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from smcity import orchestrator as orch_module

    monkeypatch.setattr(app_module, "ping", _fake_ping())
    monkeypatch.setattr(
        orch_module,
        "chat",
        _scripted_chat(
            [
                # First pass: no tools requested, just echo the input.
                LLMReply(text="echo.", tool_calls=[], usage={}, elapsed_ms=10),
            ]
            * 20
        ),
    )
    # Reroute the default SQLite DB into tmp_path so tests don't clobber local data.
    monkeypatch.setattr(app_module, "DEFAULT_DB", tmp_path / "sessions.sqlite3")


def test_health_ok() -> None:
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["llm_reachable"] is True


def test_turn_returns_detected_locale() -> None:
    with TestClient(app) as client:
        r = client.post("/turn", json={"session_id": "t1", "text": "hello"})
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == "t1"
    assert body["lang"]["source"] == "detected"


def test_turn_respects_forced_locale() -> None:
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
    with TestClient(app) as client, client.websocket_connect("/ws/s-ws-1") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "ready"
        ws.send_json({"type": "set_locale", "locale": "yue"})
        ack = ws.receive_json()
        assert ack["type"] == "locale_set"
        assert ack["locale"] == "yue"
