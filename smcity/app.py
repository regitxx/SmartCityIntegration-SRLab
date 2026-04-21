"""FastAPI entry point — Phase 0 skeleton.

- `GET /health` — liveness + LM Studio reachability.
- `POST /turn` — one-shot: calls LM Studio with a minimal system prompt and returns the reply.
- `GET /ws/{session_id}` — WebSocket echo + `set_locale` event (real orchestrator lands in Phase 1).
- `GET /` — serves the static `web/` UI shell.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from smcity import __version__
from smcity.llm import LLMError, chat, ping
from smcity.schemas import (
    Citation,
    Health,
    LanguageCoverage,
    ToolTraceEntry,
    TurnRequest,
    TurnResponse,
)
from smcity.settings import get_settings

log = structlog.get_logger("smcity")

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"


def _configure_logging() -> None:
    level = getattr(logging, get_settings().log_level.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    _configure_logging()
    s = get_settings()
    log.info("startup", base_url=s.llm_base_url, model=s.llm_model, version=__version__)
    yield
    log.info("shutdown")


app = FastAPI(
    title="smcity",
    version=__version__,
    description="HK Smart City agent — Phase 0 skeleton",
    lifespan=lifespan,
)


# ---- HTTP endpoints --------------------------------------------------------


@app.get("/health", response_model=Health)
async def health() -> Health:
    reachable, models = await ping()
    s = get_settings()
    llm_ok = reachable and s.llm_model in models
    return Health(
        status="ok" if llm_ok else "degraded",
        llm_reachable=reachable,
        llm_model=s.llm_model,
        version=__version__,
    )


_PHASE0_SYSTEM_PROMPT = (
    "You are the HK Smart City lab assistant, v0 scaffold. "
    "No tools are wired yet. Answer in the user's language. "
    "Keep replies under 2 sentences. If asked about transport or housing, "
    "say the real tools will arrive in Phase 1."
)


@app.post("/turn", response_model=TurnResponse)
async def turn(req: TurnRequest) -> TurnResponse:
    started = time.perf_counter()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _PHASE0_SYSTEM_PROMPT},
        {"role": "user", "content": req.text},
    ]

    forced = req.locale_override is not None and req.locale_override != "auto"
    reply_text: str
    tool_trace: list[ToolTraceEntry] = []
    citations: list[Citation] = []

    try:
        reply = await chat(messages)
        reply_text = reply.text or "(no reply)"
        log.info(
            "turn_llm_ok",
            session=req.session_id,
            llm_ms=reply.elapsed_ms,
            usage=reply.usage,
        )
    except LLMError as err:
        log.warning("turn_llm_error", session=req.session_id, err=str(err))
        reply_text = "(LM Studio unreachable — check Tailscale and the model on the Mac Studio)"

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return TurnResponse(
        session_id=req.session_id,
        text=reply_text,
        lang=LanguageCoverage(
            source="forced" if forced else "detected",
            primary_lang=req.locale_override or "auto",
            upstream_langs_available=[],
            translation_applied=False,
        ),
        citations=citations,
        tool_trace=tool_trace,
        elapsed_ms=elapsed_ms,
    )


# ---- WebSocket ------------------------------------------------------------


@app.websocket("/ws/{session_id}")
async def ws(session_id: str, websocket: WebSocket) -> None:
    await websocket.accept()
    await websocket.send_json(
        {
            "type": "ready",
            "session_id": session_id,
            "model": get_settings().llm_model,
            "version": __version__,
            "ts": _iso_now(),
        }
    )
    try:
        while True:
            msg = await websocket.receive_json()
            kind = msg.get("type")
            if kind == "set_locale":
                locale = str(msg.get("locale", "auto"))
                await websocket.send_json(
                    {"type": "locale_set", "locale": locale, "at": _iso_now()}
                )
                continue
            if kind == "turn":
                req = TurnRequest.model_validate(
                    {
                        "session_id": session_id,
                        "text": msg.get("text", ""),
                        "locale_override": msg.get("locale_override"),
                    }
                )
                await websocket.send_json(
                    {
                        "type": "turn.start",
                        "turn_id": _turn_id(),
                        "at": _iso_now(),
                    }
                )
                resp = await turn(req)
                await websocket.send_json(
                    {"type": "turn.final", "at": _iso_now(), "data": resp.model_dump(mode="json")}
                )
                continue
            await websocket.send_json({"type": "error", "message": f"unknown type: {kind!r}"})
    except WebSocketDisconnect:
        log.info("ws_disconnect", session=session_id)


# ---- Static UI ------------------------------------------------------------

if WEB_ROOT.exists():
    app.mount("/assets", StaticFiles(directory=WEB_ROOT, html=False), name="assets")


@app.get("/", response_model=None, include_in_schema=False)
async def index() -> FileResponse | JSONResponse:
    index_path = WEB_ROOT / "index.html"
    if not index_path.exists():
        return JSONResponse({"detail": "web/ not built yet"}, status_code=404)
    return FileResponse(index_path)


# ---- helpers ---------------------------------------------------------------


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _turn_id() -> str:
    return uuid.uuid4().hex[:12]
