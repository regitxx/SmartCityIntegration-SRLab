"""WebSocket transport — streams a turn against the agent's /ws/{session_id}.

Complements the default HTTP transport (`runner._call_agent` using POST
/turn) by exercising the same code path the production UI uses. The
streaming path yields richer telemetry:

- `ttft_ms` — wall time from "turn sent" → first `turn.token` event.
  This is the user-perceived latency that p50 targets lock on.
- `token_count` — how many incremental tokens the UI would render;
  zero indicates the agent short-circuited (fast-path chitchat or an
  error before synthesis started).
- `tool_trace` — taken from the terminal `turn.final` event so we
  match what the HTTP path would see.

The client uses the `websockets` library (already pulled in by
`uvicorn[standard]`). We keep the dependency surface narrow: one
short-lived connection per turn, explicit timeout, no retry — any
failure is recorded in `row.errors` and the campaign moves on.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from smcity_fuzz.settings import FuzzSettings


@dataclass(slots=True)
class WsTurnResult:
    reply: str
    tool_trace: list[dict[str, Any]]
    elapsed_ms: int
    ttft_ms: int | None
    token_count: int


class WsTransportError(RuntimeError):
    """Raised for any WebSocket handshake / protocol / timeout failure."""


def _ws_url(agent_url: str, session_id: str) -> str:
    base = agent_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    return f"{base}/ws/{session_id}"


async def _recv_until_final(
    ws: Any,
    *,
    timeout_s: float,
) -> tuple[list[dict[str, Any]], float | None]:
    """Drain events from the WS until `turn.final` arrives.

    Returns `(events, first_token_monotonic)` so the caller can compute
    TTFT from the moment the turn was sent.
    """
    events: list[dict[str, Any]] = []
    first_token_at: float | None = None
    deadline = time.monotonic() + timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WsTransportError(f"WS timed out after {timeout_s:.1f}s before turn.final")
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as err:
            raise WsTransportError(f"WS non-JSON frame: {err}") from err
        events.append(msg)
        if msg.get("type") == "turn.token" and first_token_at is None:
            first_token_at = time.monotonic()
        if msg.get("type") == "turn.final":
            return events, first_token_at
        if msg.get("type") == "error":
            raise WsTransportError(f"agent error over WS: {msg.get('message')}")


async def drive_turn_via_ws(
    question: str,
    session_id: str,
    *,
    settings: FuzzSettings,
    # Dependency injection for tests: any async context manager that
    # yields an object with .send() / .recv() methods.
    connect: Any = None,
) -> WsTurnResult:
    """Open /ws/{session_id}, send one turn, return aggregated metrics."""
    url = _ws_url(settings.agent_url, session_id)
    ctx = connect(url) if connect is not None else websockets.connect(url, open_timeout=10)
    start = time.monotonic()
    try:
        async with ctx as ws:
            # Drain the initial `ready` frame so it doesn't race the turn payload.
            try:
                ready_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                json.loads(ready_raw)  # we don't need the payload, just swallow it
            except TimeoutError as err:
                raise WsTransportError("WS ready frame never arrived") from err
            await ws.send(json.dumps({"type": "turn", "text": question}))
            events, first_token_at = await _recv_until_final(ws, timeout_s=settings.agent_timeout_s)
    except (InvalidHandshake, ConnectionClosed, OSError, TimeoutError) as err:
        raise WsTransportError(f"WS connect/protocol failed: {err}") from err

    reply_chunks: list[str] = []
    tool_trace: list[dict[str, Any]] = []
    elapsed_ms = 0
    for msg in events:
        if msg.get("type") == "turn.token" and msg.get("text"):
            reply_chunks.append(str(msg["text"]))
        elif msg.get("type") == "turn.final":
            data = msg.get("data") or {}
            if not reply_chunks:
                # Fast-path / non-streaming turn — take the assembled text.
                reply_chunks.append(str(data.get("text") or ""))
            tool_trace = data.get("tool_trace") or []
            elapsed_ms = int(data.get("elapsed_ms") or 0)

    token_count = sum(1 for m in events if m.get("type") == "turn.token")
    ttft_ms = int((first_token_at - start) * 1000) if first_token_at is not None else None

    return WsTurnResult(
        reply="".join(reply_chunks),
        tool_trace=tool_trace,
        elapsed_ms=elapsed_ms,
        ttft_ms=ttft_ms,
        token_count=token_count,
    )
