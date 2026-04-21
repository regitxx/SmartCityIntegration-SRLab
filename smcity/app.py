"""FastAPI entry point — Phase 1a.

- `GET /health` — liveness + LM Studio reachability.
- `POST /turn` — one-shot, orchestrator-backed.
- `GET /ws/{session_id}` — WebSocket: emits tool_call.start / tool_call.result
  events live while the agent is working.
- `GET /` — serves the static `web/` UI shell.
"""

from __future__ import annotations

import asyncio
import logging
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
from smcity.llm import ping
from smcity.orchestrator import Orchestrator, TurnEvent
from smcity.schemas import Health, TurnRequest, TurnResponse
from smcity.session import SessionStore
from smcity.settings import get_settings

log = structlog.get_logger("smcity")

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "sessions.sqlite3"


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
    DEFAULT_DB.parent.mkdir(parents=True, exist_ok=True)
    store = SessionStore(DEFAULT_DB)
    orchestrator = Orchestrator(store)
    app.state.store = store
    app.state.orchestrator = orchestrator
    log.info("startup", base_url=s.llm_base_url, model=s.llm_model, version=__version__)
    yield
    log.info("shutdown")


app = FastAPI(
    title="smcity",
    version=__version__,
    description="HK Smart City agent — Phase 1a",
    lifespan=lifespan,
)


# ---- HTTP endpoints ------------------------------------------------------


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


@app.post("/turn", response_model=TurnResponse)
async def turn(req: TurnRequest) -> TurnResponse:
    orchestrator: Orchestrator = app.state.orchestrator
    return await orchestrator.handle_turn(req)


# ---- WebSocket -----------------------------------------------------------


@app.websocket("/ws/{session_id}")
async def ws(session_id: str, websocket: WebSocket) -> None:
    await websocket.accept()
    orchestrator: Orchestrator = app.state.orchestrator
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

                emitter = _ws_emitter(websocket)
                resp = await orchestrator.handle_turn(req, emit=emitter)
                await websocket.send_json(
                    {"type": "turn.final", "at": _iso_now(), "data": resp.model_dump(mode="json")}
                )
                continue
            await websocket.send_json({"type": "error", "message": f"unknown type: {kind!r}"})
    except WebSocketDisconnect:
        log.info("ws_disconnect", session=session_id)


def _ws_emitter(websocket: WebSocket) -> Any:
    """Return a sync callable that schedules `websocket.send_json` on the running loop."""
    loop = asyncio.get_running_loop()

    def emit(event: TurnEvent) -> None:
        # turn.final is sent by the enclosing handler once the response is assembled;
        # we push only interim events here.
        if event.type == "turn.final":
            return
        payload = {"type": event.type, "at": _iso_now(), **event.data}
        asyncio.run_coroutine_threadsafe(websocket.send_json(payload), loop)

    return emit


# ---- Static UI -----------------------------------------------------------

if WEB_ROOT.exists():
    app.mount("/assets", StaticFiles(directory=WEB_ROOT, html=False), name="assets")


@app.get("/", response_model=None, include_in_schema=False)
async def index() -> FileResponse | JSONResponse:
    index_path = WEB_ROOT / "index.html"
    if not index_path.exists():
        return JSONResponse({"detail": "web/ not built yet"}, status_code=404)
    return FileResponse(index_path)


# ---- helpers -------------------------------------------------------------


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()
