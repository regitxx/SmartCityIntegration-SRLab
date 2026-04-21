"""Agent orchestrator — one turn of chat.

Flow:
1. Detect language (particle heuristic + script majority).
2. Run the deterministic pre-classifier. On a clear fast-path intent we skip
   the first LLM hop entirely.
3. Load session slots.
4. Build tool schemas + a system prompt that teaches the LLM the house rules.
5. Call gpt-oss-120b with tools + parallel_tool_calls (session_id → KV slot).
6. Execute each tool via the registry, in parallel, emitting live events.
7. Re-prompt the LLM (streaming) with tool results; yield tokens as they arrive.
8. Persist slots; return a TurnResponse with citations + tool trace.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from smcity.cantonese_polish import polish as polish_cantonese
from smcity.classifier import classify
from smcity.langrouter import DATASET_COVERAGE, LangDetection, choose_query_lang, detect
from smcity.llm import LLMError, chat, chat_stream
from smcity.prompts import (
    SYSTEM_PROMPT,
    cantonese_style_block,
    fast_path_synthesis_hint,
    language_stick_reminder,
    locale_hint,
)
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

# --- events emitted to the WebSocket --------------------------------------


@dataclass(slots=True)
class TurnEvent:
    type: str
    data: dict[str, Any] = field(default_factory=dict)


EventEmitter = Callable[[TurnEvent], Any]


# --- orchestrator ---------------------------------------------------------


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

        def _emit(ev: TurnEvent) -> None:
            if emit is not None:
                emit(ev)

        safe_text = redact_pii(req.text)
        slots = await self._store.load(req.session_id)

        carried = LangDetection(
            primary_lang=slots.locale.primary_lang,
            script="Hant" if slots.locale.script in {"Hant", "Other"} else slots.locale.script,  # type: ignore[arg-type]
            tts_locale=slots.locale.tts_locale,
            confidence=slots.locale.confidence,
            method="carried",
        )

        forced = bool(req.locale_override and req.locale_override != "auto")
        detection = (
            _detection_from_override(req.locale_override or "", carried)
            if forced
            else detect(safe_text, carried=carried)
        )

        slots.locale = Locale.from_detection(detection, forced=forced)
        if req.user_location is not None:
            slots.user_location = req.user_location

        fast_hit = classify(safe_text) if not forced else None

        _emit(
            TurnEvent(
                type="turn.start",
                data={
                    "session_id": req.session_id,
                    "detected_lang": detection.primary_lang,
                    "tts_locale": detection.tts_locale,
                    "method": detection.method,
                    "forced": forced,
                    "fast_path": fast_hit.intent if fast_hit else None,
                },
            )
        )

        tool_trace: list[ToolTraceEntry] = []
        citations: list[Citation] = []
        clarification: str | None = None

        # ---- fast path: chitchat = no tools, no LLM ----------------------
        if fast_hit and fast_hit.intent == "chitchat" and fast_hit.reply_if_chitchat:
            reply_text = _localise_chitchat(fast_hit.reply_if_chitchat, detection)
            slots.append_turn(req.text, reply_text)
            await self._store.save(slots)
            response = self._build_response(
                req,
                detection,
                forced,
                reply_text,
                citations,
                tool_trace,
                clarification,
                started,
            )
            _emit(TurnEvent(type="turn.final", data=response.model_dump(mode="json")))
            return response

        # ---- fast path: deterministic tool dispatch ----------------------
        if fast_hit and fast_hit.tools:
            tool_results = await self._run_parallel_named(fast_hit.tools, slots, detection, _emit)
            self._append_trace_and_citations(tool_results, tool_trace, citations, detection)

            # Single streaming LLM hop to synthesise the final reply from tool data.
            messages = self._build_messages(
                safe_text, detection, forced, slots, include_tools=False
            )
            serialised = "\n".join(
                f"- {r.name}: {json.dumps(r.result, ensure_ascii=False) if r.result else r.error}"
                for r in tool_results
            )
            messages.append(
                {
                    "role": "system",
                    "content": fast_path_synthesis_hint(fast_hit.intent, serialised, detection),
                }
            )
            reply_text = await self._stream_final(messages, req.session_id, _emit)
            reply_text = _maybe_polish(reply_text, detection)
            reply_text = _rewrite_source_footer(reply_text, citations)

            slots.append_turn(req.text, reply_text)
            await self._store.save(slots)
            response = self._build_response(
                req,
                detection,
                forced,
                reply_text,
                citations,
                tool_trace,
                clarification,
                started,
            )
            _emit(TurnEvent(type="turn.final", data=response.model_dump(mode="json")))
            return response

        # ---- full path: LLM picks tools, we execute, LLM synthesises -----
        messages = self._build_messages(safe_text, detection, forced, slots, include_tools=True)

        try:
            first = await chat(
                messages,
                tools=self._registry.openai_schemas(),
                parallel_tool_calls=True,
                session_id=req.session_id,
                known_tool_names=set(self._registry.names()),
            )
        except LLMError as err:
            reply_text = "(LM Studio unreachable — check Tailscale and the Mac Studio)"
            slots.append_turn(req.text, reply_text)
            await self._store.save(slots)
            response = self._build_response(
                req,
                detection,
                forced,
                reply_text,
                citations,
                tool_trace,
                clarification,
                started,
                err=str(err),
            )
            _emit(TurnEvent(type="turn.final", data=response.model_dump(mode="json")))
            return response

        if first.tool_calls:
            tool_results = await self._run_parallel(first.tool_calls, slots, detection, _emit)

            for res in tool_results:
                if res.name == "meta.ask_user" and res.status == "ok" and res.result:
                    clarification = str(res.result.get("question") or "")

            self._append_trace_and_citations(tool_results, tool_trace, citations, detection)

            messages.append(
                {
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
            )
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
            # Post-tool reminder — prevents the "Chinese in tool output pulled the
            # reply into Cantonese/Mandarin" register bug.
            messages.append({"role": "system", "content": language_stick_reminder(detection)})

            reply_text = await self._stream_final(messages, req.session_id, _emit)
            reply_text = _maybe_polish(reply_text, detection)
        else:
            reply_text = first.text or "(empty reply)"
            reply_text = _maybe_polish(reply_text, detection)

        reply_text = clarification or _rewrite_source_footer(reply_text, citations)

        slots.append_turn(req.text, reply_text)
        await self._store.save(slots)
        response = self._build_response(
            req,
            detection,
            forced,
            reply_text,
            citations,
            tool_trace,
            clarification,
            started,
        )
        _emit(TurnEvent(type="turn.final", data=response.model_dump(mode="json")))
        return response

    # --- helpers ----------------------------------------------------------

    def _build_messages(
        self,
        text: str,
        detection: LangDetection,
        forced: bool,
        slots: SessionSlots,
        *,
        include_tools: bool,
    ) -> list[dict[str, Any]]:
        msgs: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "system", "content": locale_hint(detection, forced=forced)},
        ]
        # Cantonese few-shot exemplars — only shown when the user wrote yue.
        if detection.primary_lang == "yue":
            msgs.append({"role": "system", "content": cantonese_style_block()})
        # Prior conversation so the LLM remembers origin / destination / mode
        # across turns.
        for entry in slots.history:
            msgs.append({"role": entry.role, "content": entry.content})
        msgs.append({"role": "user", "content": text})
        return msgs

    async def _stream_final(
        self,
        messages: list[dict[str, Any]],
        session_id: str,
        emit: EventEmitter,
    ) -> str:
        """Run the synthesis LLM call in streaming mode, emitting tokens live.

        Returns the cleaned final text, falling back to a retry (with a
        stronger "produce prose, not tool calls" reminder) if the synthesis
        collapsed into a tool-call-only leak.
        """
        buf: list[str] = []
        first_token_at: float | None = None
        final_text: str | None = None
        try:
            async for event in chat_stream(
                messages,
                session_id=session_id,
                known_tool_names=set(self._registry.names()),
            ):
                if event.kind == "token" and event.text:
                    if first_token_at is None:
                        first_token_at = time.perf_counter()
                        emit(TurnEvent(type="turn.llm_first_token", data={}))
                    buf.append(event.text)
                    emit(TurnEvent(type="turn.token", data={"text": event.text}))
                elif event.kind == "final":
                    final_text = event.text
        except LLMError as err:
            buf.append(f" (synthesis error: {err})")

        text = (final_text or "").strip()
        if text:
            return text

        # Synthesis collapsed (harmony/bare leaks stripped everything). Retry
        # once with a strong reminder to produce prose and no more tool calls.
        retry_messages = [
            *messages,
            {
                "role": "system",
                "content": (
                    "STOP CALLING TOOLS. Produce a short natural-language "
                    "reply (2-4 sentences) in the user's language using ONLY "
                    "the tool results already above. Do not emit any function "
                    "names, JSON, or harmony tokens."
                ),
            },
        ]
        try:
            retry = await chat(retry_messages, session_id=session_id)
            return (retry.text or "").strip() or "(I couldn't compose an answer — try rephrasing?)"
        except LLMError:
            return "(I couldn't compose an answer — try rephrasing?)"

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
            return await self._dispatch_one(name, args, slots, detection, emit)

        return await asyncio.gather(*(_one(tc) for tc in tool_calls))

    async def _run_parallel_named(
        self,
        tool_names: list[str],
        slots: SessionSlots,
        detection: LangDetection,
        emit: EventEmitter,
    ) -> list[ToolResult]:
        return await asyncio.gather(
            *(self._dispatch_one(name, {}, slots, detection, emit) for name in tool_names)
        )

    async def _dispatch_one(
        self,
        name: str,
        args: dict[str, Any],
        slots: SessionSlots,
        detection: LangDetection,
        emit: EventEmitter,
    ) -> ToolResult:
        query_lang, translated = choose_query_lang(name, detection.primary_lang, detection.script)
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

    def _append_trace_and_citations(
        self,
        tool_results: list[ToolResult],
        tool_trace: list[ToolTraceEntry],
        citations: list[Citation],
        detection: LangDetection,
    ) -> None:
        for res in tool_results:
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

    def _build_response(
        self,
        req: TurnRequest,
        detection: LangDetection,
        forced: bool,
        reply_text: str,
        citations: list[Citation],
        tool_trace: list[ToolTraceEntry],
        clarification: str | None,
        started: float,
        *,
        err: str | None = None,
    ) -> TurnResponse:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return TurnResponse(
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


# --- module-level helpers -------------------------------------------------


def _maybe_polish(text: str, detection: LangDetection) -> str:
    """Apply the deterministic formal→colloquial Cantonese pass when yue."""
    if detection.primary_lang != "yue":
        return text
    return polish_cantonese(text)


_SRC_LINE_RE = re.compile(r"(?im)^\s*src\s*[:：][^\n]*\n?")  # noqa: RUF001


def _rewrite_source_footer(text: str, citations: list[Citation]) -> str:
    """Strip any LLM-invented `src: …` line and append a deterministic one
    built from the actual citations list (so it can't be hallucinated)."""
    if not text:
        return text
    cleaned = _SRC_LINE_RE.sub("", text).rstrip()
    if not citations:
        return cleaned
    # "transport.plan_simple_route" → "plan_simple_route"
    short = [c.tool.split(".", 1)[-1] for c in citations]
    seen: set[str] = set()
    dedup: list[str] = []
    for name in short:
        if name in seen:
            continue
        seen.add(name)
        dedup.append(name)
    return f"{cleaned}\n\nsrc: {' / '.join(dedup)}"


def _localise_chitchat(canned: str, d: LangDetection) -> str:
    # Chitchat table returns a reply-per-language already; no extra logic needed.
    return canned


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
    if res.name == "transport.get_kmb_eta_by_stop":
        return f"{len(data.get('etas') or [])} KMB ETAs @ {data.get('stop_name_en')}"
    if res.name == "transport.get_citybus_eta_by_route_stop":
        return f"{len(data.get('etas') or [])} Citybus ETAs"
    if res.name == "transport.find_stops_near_point":
        return f"{len(data.get('stops') or [])} stops"
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
    if res.name == "facility.find_nearby_courts":
        return f"{len(data.get('courts') or [])} courts"
    if res.name == "facility.find_nearby_pools":
        return f"{len(data.get('pools') or [])} pools"
    if res.name == "housing.get_estate_info":
        m = data.get("match")
        return m.get("name_en") if isinstance(m, dict) else None
    return None


def _langs_of(trace: list[ToolTraceEntry]) -> list[str]:
    langs: set[str] = set()
    for t in trace:
        if t.status == "ok":
            langs.update(DATASET_COVERAGE.get(t.name, set()))
    return sorted(langs)
