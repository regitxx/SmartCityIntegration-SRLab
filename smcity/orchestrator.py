"""Agent orchestrator — one turn of chat.

Flow:
1. Detect language (particle heuristic + script majority).
2. Normalise for upstream queries if needed (OpenCC s2hk).
3. Load session slots.
4. Build tool schemas + a system prompt that teaches the LLM the house rules.
5. Call gpt-oss-120b with tools + parallel_tool_calls.
6. Execute each tool via the registry, in parallel, emitting live events.
7. Re-prompt the LLM with tool results for the final reply.
8. Persist slots; return a TurnResponse with citations + tool trace.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from smcity.langrouter import DATASET_COVERAGE, LangDetection, choose_query_lang, detect
from smcity.llm import LLMError, chat
from smcity.schemas import (
    Citation,
    LanguageCoverage,
    ToolTraceEntry,
    TurnRequest,
    TurnResponse,
)
from smcity.session import SessionStore, redact_pii
from smcity.slots import Locale, SessionSlots
from smcity.tools import ToolRegistry, build_default_registry
from smcity.tools.registry import ToolContext, ToolResult

# --- system prompt --------------------------------------------------------

_SYSTEM_PROMPT = """You are the Hong Kong smart-city assistant for the Lab of \
Social Robotics. You help users with transportation (MTR, bus, minibus, tram, \
ferry, taxi, walking), public facilities, housing, weather, air quality, and \
related questions in any language.

Principles:
- Cantonese is the priority language. When the user writes in Cantonese, reply \
in natural written Cantonese (using 嘅 / 喺 / 咗 / 冇 / 佢 / 唔 etc.). Never \
silently convert to Mandarin.
- Answer in the user's language unless they explicitly switched.
- Every factual claim about HK city state comes from a tool call. Do not invent \
MTR stations, bus routes, weather numbers, AQHI bands, or addresses.
- When origin / destination / transport mode / venue type is missing or \
ambiguous, call meta.ask_user with ONE short question. Do not ask multiple \
questions in one turn.
- For travel queries, parallelise context tools (weather + warnings + AQHI) \
with the transport tool in ONE tool-calls batch.
- Keep final replies short (2-4 sentences) unless the user asks for detail.
- At the end of every user-facing reply, include a one-line source footer such \
as "src: mtr_next_trains · hko_warnings · 14:03".

Tools are listed separately. Call them when useful; answer directly only for \
conversational pleasantries."""


# --- events emitted to the WebSocket --------------------------------------


@dataclass(slots=True)
class TurnEvent:
    type: str  # "turn.start" | "tool_call.start" | "tool_call.result" | "turn.final"
    data: dict[str, Any] = field(default_factory=dict)


EventEmitter = Callable[[TurnEvent], Any]  # fire-and-forget


# --- orchestrator ---------------------------------------------------------


@dataclass(slots=True)
class OrchestratorResult:
    response: TurnResponse
    events: list[TurnEvent]


class Orchestrator:
    def __init__(
        self,
        store: SessionStore,
        registry: ToolRegistry | None = None,
    ) -> None:
        self._store = store
        self._registry = registry or build_default_registry()

    async def handle_turn(
        self,
        req: TurnRequest,
        *,
        emit: EventEmitter | None = None,
    ) -> TurnResponse:
        started = time.perf_counter()
        events: list[TurnEvent] = []

        def _emit(ev: TurnEvent) -> None:
            events.append(ev)
            if emit is not None:
                emit(ev)

        # 1) PII scrub + language detect
        safe_text = redact_pii(req.text)
        slots = await self._store.load(req.session_id)

        carried = LangDetection(
            primary_lang=slots.locale.primary_lang,
            script="Hant" if slots.locale.script in {"Hant", "Other"} else slots.locale.script,  # type: ignore[arg-type]
            tts_locale=slots.locale.tts_locale,
            confidence=slots.locale.confidence,
            method="carried",
        )

        forced = req.locale_override and req.locale_override != "auto"
        if forced:
            detection = _detection_from_override(req.locale_override or "", carried)
        else:
            detection = detect(safe_text, carried=carried)

        slots.locale = Locale.from_detection(detection, forced=bool(forced))
        if req.user_location is not None:
            slots.user_location = req.user_location

        _emit(
            TurnEvent(
                type="turn.start",
                data={
                    "session_id": req.session_id,
                    "detected_lang": detection.primary_lang,
                    "tts_locale": detection.tts_locale,
                    "method": detection.method,
                    "forced": bool(forced),
                },
            )
        )

        # 2) First LLM pass — may request tool calls
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "system", "content": _locale_hint(detection)},
            {"role": "user", "content": safe_text},
        ]

        try:
            first = await chat(
                messages, tools=self._registry.openai_schemas(), parallel_tool_calls=True
            )
        except LLMError as err:
            return self._degraded(req, slots, detection, started, events, str(err))

        tool_trace: list[ToolTraceEntry] = []
        citations: list[Citation] = []
        clarification: str | None = None

        if first.tool_calls:
            # 3) Execute every tool call in parallel
            tool_results = await self._run_parallel(first.tool_calls, slots, detection, _emit)

            # Extract ask_user if present — that's our clarification gate
            for res in tool_results:
                if res.name == "meta.ask_user" and res.status == "ok" and res.result:
                    clarification = str(res.result.get("question") or "")

            # 4) Second LLM pass with tool outputs
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": first.text or "",
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": tc["arguments"]},
                    }
                    for tc in first.tool_calls
                ],
            }
            messages.append(assistant_msg)
            for res in tool_results:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": _id_for(first.tool_calls, res.name, res.args),
                        "content": json.dumps(
                            {"status": res.status, "result": res.result, "error": res.error},
                            ensure_ascii=False,
                        ),
                    }
                )
                tool_trace.append(
                    ToolTraceEntry(
                        index=len(tool_trace) + 1,
                        name=res.name,
                        args=res.args,
                        status=res.status,  # type: ignore[arg-type]
                        latency_ms=res.latency_ms,
                        result_summary=_summarise(res),
                    )
                )
                if res.status == "ok":
                    spec = self._registry.get(res.name)
                    citations.append(
                        Citation(
                            tool=res.name,
                            upstream=spec.upstream or "(local)",
                            fetched_at=datetime.now(UTC),
                            upstream_langs=sorted(spec.upstream_langs),
                            translation_applied=_translation_flag(res.name, detection),
                        )
                    )

            try:
                second = await chat(messages)
                reply_text = second.text or first.text or "(no reply)"
            except LLMError as err:
                reply_text = f"(LLM error in follow-up: {err})"
        else:
            reply_text = first.text or "(empty reply)"

        if clarification:
            # If meta.ask_user fired, prefer surfacing the clarification exactly.
            reply_text = clarification

        await self._store.save(slots)

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        response = TurnResponse(
            session_id=req.session_id,
            text=reply_text,
            lang=LanguageCoverage(
                source="forced" if forced else "detected",
                primary_lang=detection.primary_lang,
                upstream_langs_available=_langs_of(tool_trace),
                translation_applied=any(c.translation_applied for c in citations),
            ),
            citations=citations,
            tool_trace=tool_trace,
            followups=[clarification] if clarification else [],
            elapsed_ms=elapsed_ms,
        )
        _emit(TurnEvent(type="turn.final", data=response.model_dump(mode="json")))
        return response

    # --- helpers ----------------------------------------------------------

    async def _run_parallel(
        self,
        tool_calls: list[dict[str, Any]],
        slots: SessionSlots,
        detection: LangDetection,
        emit: EventEmitter,
    ) -> list[ToolResult]:
        async def _one(tc: dict[str, Any]) -> ToolResult:
            name = tc["name"]
            try:
                args = json.loads(tc.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            query_lang, translated = choose_query_lang(
                name, detection.primary_lang, detection.script
            )
            ctx = ToolContext(
                session_id=slots.session_id,
                locale=detection.primary_lang,
                query_lang=query_lang,
                translation_applied=translated,
            )
            emit(
                TurnEvent(
                    type="tool_call.start",
                    data={"name": name, "args": args, "query_lang": query_lang},
                )
            )
            result = await self._registry.dispatch(name, args, ctx)
            emit(
                TurnEvent(
                    type="tool_call.result",
                    data={
                        "name": name,
                        "status": result.status,
                        "latency_ms": result.latency_ms,
                        "error": result.error,
                    },
                )
            )
            return result

        return await asyncio.gather(*(_one(tc) for tc in tool_calls))

    def _degraded(
        self,
        req: TurnRequest,
        slots: SessionSlots,
        detection: LangDetection,
        started: float,
        events: list[TurnEvent],
        err: str,
    ) -> TurnResponse:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        msg = "(LM Studio unreachable — check Tailscale and the Mac Studio)"
        return TurnResponse(
            session_id=req.session_id,
            text=msg,
            lang=LanguageCoverage(
                source="forced" if req.locale_override else "detected",
                primary_lang=detection.primary_lang,
                upstream_langs_available=[],
                translation_applied=False,
            ),
            citations=[],
            tool_trace=[],
            followups=[],
            elapsed_ms=elapsed_ms,
        )


# --- module-level helpers -------------------------------------------------


def _locale_hint(d: LangDetection) -> str:
    return (
        f"User language: primary_lang={d.primary_lang!r} script={d.script!r} "
        f"tts_locale={d.tts_locale!r}. Reply in this language. If user wrote "
        f"Cantonese (yue), use natural written Cantonese with particles like 嘅/喺/咗/冇/佢/唔."
    )


def _detection_from_override(code: str, carried: LangDetection) -> LangDetection:
    mapping = {
        "yue": ("yue", "Hant", "yue-HK"),
        "zh-Hant": ("zho", "Hant", "zh-HK"),
        "zh-Hans": ("zho", "Hans", "zh-CN"),
        "en": ("eng", "Latin", "en-US"),
        "ja": ("jpn", "Hiragana", "ja-JP"),
        "ko": ("kor", "Hangul", "ko-KR"),
        "fr": ("fra", "Latin", "fr-FR"),
        "de": ("deu", "Latin", "de-DE"),
        "es": ("spa", "Latin", "es-ES"),
        "th": ("tha", "Thai", "th-TH"),
        "tl": ("tgl", "Latin", "tl-PH"),
        "id": ("ind", "Latin", "id-ID"),
        "vi": ("vie", "Latin", "vi-VN"),
    }
    primary, script, tts = mapping.get(
        code, (carried.primary_lang, carried.script, carried.tts_locale)
    )
    return LangDetection(
        primary_lang=primary,
        script=script,  # type: ignore[arg-type]
        confidence=1.0,
        method="forced",
        tts_locale=tts,
    )


def _translation_flag(tool_name: str, detection: LangDetection) -> bool:
    _, translated = choose_query_lang(tool_name, detection.primary_lang, detection.script)
    return translated


def _id_for(tool_calls: list[dict[str, Any]], name: str, args: dict[str, Any]) -> str:
    for tc in tool_calls:
        if tc["name"] == name:
            try:
                parsed = json.loads(tc.get("arguments") or "{}")
            except json.JSONDecodeError:
                parsed = {}
            if parsed == args:
                return str(tc.get("id", name))
    return name


def _summarise(res: ToolResult) -> str | None:
    if res.status != "ok" or not res.result:
        return res.error
    data = res.result
    if res.name == "transport.get_mtr_next_trains":
        trains = data.get("next_trains") or []
        return f"{len(trains)} trains @ {data.get('station_name_en')}"
    if res.name == "context.get_current_weather":
        return f"{data.get('temperature_c')}°C / {data.get('humidity_pct')}% RH"
    if res.name == "context.get_active_warnings":
        warnings = data.get("warnings") or []
        return f"{len(warnings)} active" if warnings else "none"
    if res.name == "context.get_aqhi":
        stations = data.get("stations") or []
        return f"{len(stations)} stations"
    if res.name == "geo.address_lookup":
        candidates = data.get("candidates") or []
        return f"{len(candidates)} candidates"
    return None


def _langs_of(trace: list[ToolTraceEntry]) -> list[str]:
    langs: set[str] = set()
    for t in trace:
        if t.status == "ok":
            langs.update(DATASET_COVERAGE.get(t.name, set()))
    return sorted(langs)
