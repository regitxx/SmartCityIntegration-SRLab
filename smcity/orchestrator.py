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
from smcity.chain_rules import (
    AutoDispatch,
    ChainContinuation,
    apply_chain_rules,
)
from smcity.classifier import classify
from smcity.langrouter import DATASET_COVERAGE, LangDetection, choose_query_lang, detect
from smcity.llm import LLMError, LLMReply, chat, chat_stream
from smcity.observability import get_tracer, set_attr_safe
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
from smcity.synthesis_invariants import apply_invariants
from smcity.tool_call_gates import GateViolation, apply_gates
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
        tracer = get_tracer("smcity.orchestrator")
        # The turn span wraps the whole user-request lifecycle. Phoenix groups
        # children (llm.chat, tool.dispatch, outbound httpx) under it; the
        # `session.id` attribute is what Phoenix uses to bucket history per
        # session, which is exactly what the boss asked for ("new session on
        # page refresh = new history").
        with tracer.start_as_current_span(
            "smcity.turn",
            attributes={
                "session.id": req.session_id,
                "user.text": req.text[:1024],
                "locale_override": req.locale_override or "auto",
            },
        ) as turn_span:
            response = await self._handle_turn_inner(req, emit, turn_span)
            set_attr_safe(turn_span, "reply.text", response.text[:1024])
            set_attr_safe(turn_span, "detected_lang", response.lang.primary_lang)
            set_attr_safe(turn_span, "tool_count", len(response.tool_trace))
            set_attr_safe(turn_span, "citations_count", len(response.citations))
            return response

    async def _handle_turn_inner(
        self,
        req: TurnRequest,
        emit: EventEmitter | None,
        _turn_span: Any,  # passed through for child spans
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
            reply_text = _normalise_whitespace(reply_text)
            reply_text = await self._maybe_retry_for_invariants(
                reply_text,
                messages,
                tool_results,
                detection,
                req.session_id,
                _emit,
            )
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
            with get_tracer("smcity.orchestrator").start_as_current_span("llm.chat.decide"):
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

        # --- Pre-execution gates ----------------------------------------
        # Declarative checks in smcity/tool_call_gates.py reject obviously
        # bad tool-call proposals (e.g., leading with meta.ask_user when no
        # search has been tried). On a violation, re-prompt the LLM once
        # with the corrective hint and use the retry's tool calls.
        #
        # If the retry STILL violates the same gate (gpt-oss-120b on
        # Cantonese POI queries occasionally ignores the corrective hint
        # and re-emits the same bare-find_poi shape), we escalate to
        # deterministic rectification — substituting a known-good shape
        # for the bad one. Currently only `missing_spatial_scope` has a
        # rectification path; other gate kinds fall through and accept
        # whatever the retry produced.
        if first.tool_calls:
            gate_violation = apply_gates(first.tool_calls)
            if gate_violation is not None:
                _emit(
                    TurnEvent(
                        type="gate.violated",
                        data={"name": gate_violation.name, "kind": gate_violation.kind},
                    )
                )
                retry_messages = [
                    *messages,
                    {
                        "role": "assistant",
                        "content": first.text or "",
                        "tool_calls": [
                            {
                                "id": tc["id"],
                                "type": "function",
                                "function": {
                                    "name": tc["name"],
                                    "arguments": tc["arguments"],
                                },
                            }
                            for tc in first.tool_calls
                        ],
                    },
                    {"role": "system", "content": gate_violation.corrective_prompt},
                ]
                try:
                    with get_tracer("smcity.orchestrator").start_as_current_span(
                        "llm.chat.gate_retry"
                    ):
                        retry = await chat(
                            retry_messages,
                            tools=self._registry.openai_schemas(),
                            parallel_tool_calls=True,
                            session_id=req.session_id,
                            known_tool_names=set(self._registry.names()),
                        )
                except LLMError:
                    retry = None
                if retry is not None and retry.tool_calls:
                    # Re-check the gate. If the retry STILL violates the
                    # same gate, apply deterministic rectification (when we
                    # have a rectification path for this gate kind).
                    retry_violation = apply_gates(retry.tool_calls)
                    if retry_violation is not None and retry_violation.kind == gate_violation.kind:
                        rectified = _rectify(retry_violation, retry.tool_calls, safe_text)
                        if rectified is not None:
                            _emit(
                                TurnEvent(
                                    type="gate.rectified",
                                    data={
                                        "name": retry_violation.name,
                                        "kind": retry_violation.kind,
                                    },
                                )
                            )
                            retry = LLMReply(
                                text=retry.text,
                                tool_calls=rectified,
                                usage=retry.usage,
                                elapsed_ms=retry.elapsed_ms,
                            )
                    first = retry

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

            # --- Chain-completion enforcement -------------------------------
            # Declarative rules in smcity/chain_rules.py decide whether the
            # tool chain is incomplete and how to finish it. Two outcomes:
            #
            # AutoDispatch — orchestrator fires the successor tool directly
            #   (deterministic, no LLM re-roll). Used when the missing tool +
            #   args can be inferred unambiguously from the user's text and
            #   the precondition result. Currently: POI category inference.
            #
            # LLMHint — orchestrator appends a system message and re-prompts
            #   the LLM to pick the successor. Used when inference is too
            #   ambiguous. This is the old Fix 3 path, preserved as fallback.
            # Track the union of all dispatched tool results — first round
            # plus anything the chain-rules engine adds — so the synthesis
            # invariant check sees the full picture.
            all_tool_results: list[ToolResult] = list(tool_results)

            chain_match = apply_chain_rules(safe_text, tool_results)
            if chain_match is not None:
                rule, continuation = chain_match
                followup_results = await self._apply_continuation(
                    rule.name,
                    continuation,
                    messages,
                    tool_trace,
                    citations,
                    slots,
                    detection,
                    req.session_id,
                    _emit,
                )
                all_tool_results.extend(followup_results)

            # Post-tool reminder — prevents the "Chinese in tool output pulled the
            # reply into Cantonese/Mandarin" register bug.
            messages.append({"role": "system", "content": language_stick_reminder(detection)})

            reply_text = await self._stream_final(messages, req.session_id, _emit)
            reply_text = _normalise_whitespace(reply_text)
            # Synthesis invariant check — catches "tool returned data, reply
            # denies it" before we ever ship the bad text. See
            # smcity/synthesis_invariants.py.
            reply_text = await self._maybe_retry_for_invariants(
                reply_text,
                messages,
                all_tool_results,
                detection,
                req.session_id,
                _emit,
            )
            reply_text = _maybe_polish(reply_text, detection)
        else:
            reply_text = first.text or "(empty reply)"
            reply_text = _normalise_whitespace(reply_text)
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
        with get_tracer("smcity.orchestrator").start_as_current_span("llm.chat.synthesis"):
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
            with get_tracer("smcity.orchestrator").start_as_current_span(
                "llm.chat.synthesis_retry"
            ):
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

    async def _apply_continuation(
        self,
        rule_name: str,
        continuation: ChainContinuation,
        messages: list[dict[str, Any]],
        tool_trace: list[ToolTraceEntry],
        citations: list[Citation],
        slots: SessionSlots,
        detection: LangDetection,
        session_id: str,
        emit: EventEmitter,
    ) -> list[ToolResult]:
        """Apply a ChainContinuation produced by smcity/chain_rules.py.

        AutoDispatch — dispatch the named tool ourselves and append a
        synthetic assistant+tool message pair so the synthesis LLM sees the
        successor result alongside the original tool calls.

        LLMHint — append the hint text as a system message, re-prompt the
        LLM, and execute whatever tool calls it returns (the old Fix 3 path).

        Returns the ToolResults that were actually dispatched (so the caller
        can include them in the post-synthesis invariant check). Empty when
        the LLM-hint retry returned no tool calls.
        """
        emit(
            TurnEvent(
                type="chain.fired",
                data={
                    "rule": rule_name,
                    "kind": "auto_dispatch"
                    if isinstance(continuation, AutoDispatch)
                    else "llm_hint",
                },
            )
        )

        if isinstance(continuation, AutoDispatch):
            followup = await self._dispatch_one(
                continuation.tool, continuation.args, slots, detection, emit
            )
            self._append_trace_and_citations([followup], tool_trace, citations, detection)
            synthetic_id = f"chain-{rule_name}-{continuation.tool}"
            messages.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": synthetic_id,
                            "type": "function",
                            "function": {
                                "name": continuation.tool,
                                "arguments": json.dumps(continuation.args, ensure_ascii=False),
                            },
                        }
                    ],
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": synthetic_id,
                    "content": json.dumps(
                        {
                            "status": followup.status,
                            "result": followup.result,
                            "error": followup.error,
                        },
                        ensure_ascii=False,
                    ),
                }
            )
            return [followup]

        # LLMHint — re-prompt the LLM with the hint appended.
        messages.append({"role": "system", "content": continuation.text})
        try:
            with get_tracer("smcity.orchestrator").start_as_current_span(
                "llm.chat.chain_rules_retry"
            ):
                retry = await chat(
                    messages,
                    tools=self._registry.openai_schemas(),
                    parallel_tool_calls=True,
                    session_id=session_id,
                    known_tool_names=set(self._registry.names()),
                )
        except LLMError:
            return []
        if not retry.tool_calls:
            return []
        retry_results = await self._run_parallel(retry.tool_calls, slots, detection, emit)
        self._append_trace_and_citations(retry_results, tool_trace, citations, detection)
        messages.append(
            {
                "role": "assistant",
                "content": retry.text or "",
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": tc["arguments"]},
                    }
                    for tc in retry.tool_calls
                ],
            }
        )
        for res in retry_results:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": _id_for(retry.tool_calls, res.name, res.args),
                    "content": json.dumps(
                        {"status": res.status, "result": res.result, "error": res.error},
                        ensure_ascii=False,
                    ),
                }
            )
        return retry_results

    async def _maybe_retry_for_invariants(
        self,
        reply_text: str,
        messages: list[dict[str, Any]],
        tool_results: list[ToolResult],
        detection: LangDetection,
        session_id: str,
        emit: EventEmitter,
    ) -> str:
        """Run synthesis invariants over `reply_text`. If a violation fires,
        append the corrective system message and re-prompt the LLM once
        (non-streaming). Return the retry text on success, the original on
        any failure — we never make the reply worse by trying to fix it.

        Sibling to the chain-rules engine: chain_rules guards the tool-call
        stage; this guards the synthesis stage. Both are structural.
        """
        violation = apply_invariants(reply_text, tool_results, detection)
        if violation is None:
            return reply_text

        emit(
            TurnEvent(
                type="invariant.violated",
                data={
                    "name": violation.name,
                    "kind": violation.kind,
                    "tool": violation.tool_name,
                    "records": violation.record_count,
                },
            )
        )

        retry_messages = [
            *messages,
            {"role": "assistant", "content": reply_text},
            {"role": "system", "content": violation.corrective_prompt},
        ]
        try:
            with get_tracer("smcity.orchestrator").start_as_current_span(
                "llm.chat.invariant_retry"
            ):
                retry = await chat(
                    retry_messages,
                    session_id=session_id,
                    known_tool_names=set(self._registry.names()),
                )
        except LLMError:
            return reply_text
        retry_text = (retry.text or "").strip()
        return retry_text or reply_text

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
                    result=res.result if res.status == "ok" else None,
                )
            )
            if res.status == "ok":
                spec = self._registry.get(res.name)
                discriminator: str | None = None
                if spec.citation_discriminator_key and isinstance(res.result, dict):
                    candidate = res.result.get(spec.citation_discriminator_key)
                    if isinstance(candidate, str) and candidate:
                        discriminator = candidate
                citations.append(
                    Citation(
                        tool=res.name,
                        upstream=spec.upstream or "(local)",
                        fetched_at=datetime.now(UTC),
                        upstream_langs=sorted(spec.upstream_langs),
                        translation_applied=_translation_flag(res.name, detection),
                        discriminator=discriminator,
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

# Unicode whitespace characters that gpt-oss-120b inserts as "typography
# flourish" — narrow no-break space (U+202F) between paired words like
# "Mong Kok", figure space (U+2007), etc. They render as regular spaces in
# every browser but break naive substring search (`"Mong Kok" in reply`
# fails) and copy-paste workflows. Normalised to a regular ASCII space in
# the final reply.
_UNICODE_SPACE_NORMALISE = str.maketrans(
    {
        "\u00a0": " ",  # NO-BREAK SPACE
        "\u202f": " ",  # NARROW NO-BREAK SPACE
        "\u2007": " ",  # FIGURE SPACE
        "\u2009": " ",  # THIN SPACE
        "\u200a": " ",  # HAIR SPACE
    }
)


def _normalise_whitespace(text: str) -> str:
    """Replace exotic Unicode space characters with ASCII space."""
    return text.translate(_UNICODE_SPACE_NORMALISE)


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
    # "transport.plan_simple_route" → "plan_simple_route"; for tools that
    # carry a discriminator (geo.find_poi), append it so the footer keeps
    # the user-facing specificity (find_poi/dentist) it had before v0.6.0
    # collapsed the per-category POI tools.
    short: list[str] = []
    for c in citations:
        base = c.tool.split(".", 1)[-1]
        short.append(f"{base}/{c.discriminator}" if c.discriminator else base)
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


def _rectify(
    violation: GateViolation,
    proposed_calls: list[dict[str, Any]],
    user_text: str,
) -> list[dict[str, Any]] | None:
    """Build a deterministic substitute tool-call list for the given gate
    violation, or None if the gate kind has no rectification path.

    Currently implements one rectification:
    - `missing_spatial_scope` — drop the bare `geo.find_poi` call, substitute
      `geo.address_lookup(query=user_text)`. The chain_rules POI engine then
      auto-dispatches `geo.find_poi(category=..., lat=..., lng=...)` once the
      lookup resolves coords.
    """
    if violation.kind != "missing_spatial_scope":
        return None
    kept = [c for c in proposed_calls if c.get("name") != "geo.find_poi"]
    # Preserve any sibling tools the LLM correctly proposed (e.g. context
    # checks paralleled with the bad find_poi); add the lookup.
    has_lookup = any(c.get("name") == "geo.address_lookup" for c in kept)
    if not has_lookup:
        kept.append(
            {
                "id": "rectify-address-lookup",
                "name": "geo.address_lookup",
                "arguments": json.dumps({"query": user_text}, ensure_ascii=False),
            }
        )
    return kept


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
