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
    """The ask_user_only_first_move gate fires here — but if the LLM
    persists with the same ask_user proposal on the retry (or the retry
    returns no tool calls), the orchestrator accepts it and surfaces the
    clarification as before. Documents the gate-friendly fallback path."""
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
    # Gate retry — LLM persists with the same ask_user proposal.
    gate_retry = LLMReply(
        text="",
        tool_calls=[
            {
                "id": "call-ask-retry",
                "name": "meta.ask_user",
                "arguments": json.dumps({"question": "搭 MTR、巴士定的士？", "slot": "mode"}),
            }
        ],
        usage={},
        elapsed_ms=70,
    )
    second = LLMReply(text="(synthesised)", tool_calls=[], usage={}, elapsed_ms=40)
    from smcity import orchestrator as orch_module

    monkeypatch.setattr(orch_module, "chat", _scripted_chat([first, gate_retry, second]))

    store = SessionStore(tmp_path / "sessions.sqlite3")
    orch = Orchestrator(store)
    req = TurnRequest(session_id="clarify-1", text="我想由上環去沙田")
    resp = await orch.handle_turn(req)

    assert "MTR" in resp.text
    assert resp.followups and "MTR" in resp.followups[0]


@pytest.mark.asyncio
@respx.mock
async def test_ask_user_gate_redirects_llm_to_search_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate's success case: LLM proposed ask_user alone, gate fired,
    LLM's retry picked a real search tool (transport.get_mtr_next_trains),
    and the orchestrator dispatched THAT tool — never executing ask_user."""
    respx.get(MTR_NEXT_TRAIN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": 1,
                "data": {
                    "ISL-CEN": {
                        "UP": [{"dest": "CHW", "ttnt": "2", "seq": 1, "plat": "1"}],
                        "DOWN": [{"dest": "KET", "ttnt": "3", "seq": 1, "plat": "2"}],
                    }
                },
            },
        )
    )
    bad_first = LLMReply(
        text="",
        tool_calls=[
            {
                "id": "call-ask",
                "name": "meta.ask_user",
                "arguments": json.dumps({"question": "Which station?", "slot": "venue_type"}),
            }
        ],
        usage={},
        elapsed_ms=80,
    )
    # Gate retry — LLM gets it right this time, calls the real tool.
    good_retry = LLMReply(
        text="",
        tool_calls=[
            {
                "id": "call-mtr",
                "name": "transport.get_mtr_next_trains",
                "arguments": json.dumps({"station_name": "Central"}),
            }
        ],
        usage={},
        elapsed_ms=90,
    )
    synth = LLMReply(text="Next train in 2 min.", tool_calls=[], usage={}, elapsed_ms=30)
    from smcity import orchestrator as orch_module

    monkeypatch.setattr(orch_module, "chat", _scripted_chat([bad_first, good_retry, synth]))

    store = SessionStore(tmp_path / "sessions.sqlite3")
    orch = Orchestrator(store)
    req = TurnRequest(session_id="gate-redirect-1", text="next train at central")
    resp = await orch.handle_turn(req)

    # Only the redirected tool was executed; ask_user was never dispatched.
    executed_tools = [t.name for t in resp.tool_trace]
    assert "transport.get_mtr_next_trains" in executed_tools
    assert "meta.ask_user" not in executed_tools
    # No followup was set because no ask_user fired.
    assert not resp.followups


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


@pytest.mark.asyncio
@respx.mock
async def test_chain_rule_auto_dispatches_poi_followup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end proof of the chain_rules AutoDispatch path:
    LLM calls geo.address_lookup → chain rule infers 'dentist' from user
    text → orchestrator deterministically dispatches geo.find_poi with
    category='dentist' and the resolved lat/lng → both tools appear in
    tool_trace. No LLM re-roll is needed for the chain step (the AutoDispatch
    path is deterministic)."""
    # ALS upstream — returns Tsim Sha Tsui at 22.30, 114.17.
    respx.get("https://www.als.gov.hk/lookup").mock(
        return_value=httpx.Response(
            200,
            json={
                "SuggestedAddress": [
                    {
                        "Address": {
                            "PremisesAddress": {
                                "EngPremisesAddress": {
                                    "BuildingName": "Tsim Sha Tsui",
                                    "EngDistrict": {"DcDistrict": "Yau Tsim Mong"},
                                },
                                "ChiPremisesAddress": {"BuildingName": "尖沙咀"},
                                "GeospatialInformation": {
                                    "Latitude": "22.30",
                                    "Longitude": "114.17",
                                },
                            }
                        }
                    }
                ]
            },
        )
    )
    # Overpass upstream — returns two dentists near the resolved point.
    respx.post("https://overpass-api.de/api/interpreter").mock(
        return_value=httpx.Response(
            200,
            json={
                "elements": [
                    {
                        "type": "node",
                        "id": 1001,
                        "lat": 22.301,
                        "lon": 114.171,
                        "tags": {"name": "Dr Chan Dental Clinic", "amenity": "dentist"},
                    },
                    {
                        "type": "node",
                        "id": 1002,
                        "lat": 22.302,
                        "lon": 114.172,
                        "tags": {"name": "Wong Dental Surgery", "amenity": "dentist"},
                    },
                ]
            },
        )
    )

    # LLM call 1: fires address_lookup only — the chain rule will infer the
    # POI category from "dentist" in the user text and AutoDispatch find_dentist.
    first = LLMReply(
        text="",
        tool_calls=[
            {
                "id": "call-addr",
                "name": "geo.address_lookup",
                "arguments": json.dumps({"query": "Tsim Sha Tsui"}),
            }
        ],
        usage={},
        elapsed_ms=80,
    )
    # Synthesis fallback (chat_stream is unmocked → empty → chat() fallback).
    synth = LLMReply(
        text="Two dentists near TST: Dr Chan Dental Clinic and Wong Dental Surgery.",
        tool_calls=[],
        usage={},
        elapsed_ms=40,
    )
    from smcity import orchestrator as orch_module

    monkeypatch.setattr(orch_module, "chat", _scripted_chat([first, synth]))

    store = SessionStore(tmp_path / "sessions.sqlite3")
    orch = Orchestrator(store)
    req = TurnRequest(
        session_id="chain-poi-1",
        text="where's the nearest dentist near TST?",
        locale_override="en",
    )

    events: list[Any] = []
    resp = await orch.handle_turn(req, emit=events.append)

    # Both tools must have run successfully.
    tool_names = [t.name for t in resp.tool_trace]
    assert "geo.address_lookup" in tool_names
    assert "geo.find_poi" in tool_names
    # AutoDispatch must have used category='dentist'.
    poi_entries = [t for t in resp.tool_trace if t.name == "geo.find_poi"]
    assert poi_entries and poi_entries[0].args.get("category") == "dentist"
    for t in resp.tool_trace:
        assert t.status == "ok", f"expected ok, got {t.name}={t.status}"
    # Citation footer must preserve the category sub-type.
    poi_citations = [c for c in resp.citations if c.tool == "geo.find_poi"]
    assert poi_citations and poi_citations[0].discriminator == "dentist"
    assert "find_poi/dentist" in resp.text

    # The chain.fired event identifies the rule + the deterministic kind.
    chain_events = [e for e in events if e.type == "chain.fired"]
    assert len(chain_events) == 1, f"expected one chain.fired event, got {len(chain_events)}"
    assert chain_events[0].data["rule"] == "poi_address_to_find"
    assert chain_events[0].data["kind"] == "auto_dispatch"


@pytest.mark.asyncio
@respx.mock
async def test_synthesis_invariant_retries_on_data_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end proof of the synthesis_invariants retry path:
    tool returns two dentists, the LLM's synthesis denies the data
    ("I couldn't find any"), the data_denial invariant fires, a corrective
    retry produces a reply that cites at least one record. The bad reply
    is replaced by the good one before it ships to the user."""
    respx.post("https://overpass-api.de/api/interpreter").mock(
        return_value=httpx.Response(
            200,
            json={
                "elements": [
                    {
                        "type": "node",
                        "id": 1001,
                        "lat": 22.30,
                        "lon": 114.17,
                        "tags": {"name": "Dr Chan Dental Clinic", "amenity": "dentist"},
                    },
                    {
                        "type": "node",
                        "id": 1002,
                        "lat": 22.30,
                        "lon": 114.17,
                        "tags": {"name": "Wong Dental Surgery", "amenity": "dentist"},
                    },
                ]
            },
        )
    )

    # User gives coords directly so the chain rule has no precondition
    # (no address_lookup fires) — keeps this test focused on the invariant.
    first = LLMReply(
        text="",
        tool_calls=[
            {
                "id": "call-find",
                "name": "geo.find_poi",
                "arguments": json.dumps(
                    {"category": "dentist", "lat": 22.30, "lng": 114.17}
                ),
            }
        ],
        usage={},
        elapsed_ms=80,
    )
    # Synthesis fallback denies the data — should trip the invariant.
    bad_synth = LLMReply(
        text="I couldn't find any dentists in your area, sorry.",
        tool_calls=[],
        usage={},
        elapsed_ms=40,
    )
    # Corrective retry — cites the first record.
    good_synth = LLMReply(
        text="Dr Chan Dental Clinic is right nearby.",
        tool_calls=[],
        usage={},
        elapsed_ms=40,
    )
    from smcity import orchestrator as orch_module

    monkeypatch.setattr(orch_module, "chat", _scripted_chat([first, bad_synth, good_synth]))

    store = SessionStore(tmp_path / "sessions.sqlite3")
    orch = Orchestrator(store)
    req = TurnRequest(
        session_id="invariant-1",
        text="dentist at lat 22.30 lng 114.17",
        locale_override="en",
    )

    events: list[Any] = []
    resp = await orch.handle_turn(req, emit=events.append)

    # The invariant must have fired with the right metadata.
    inv_events = [e for e in events if e.type == "invariant.violated"]
    assert len(inv_events) == 1, f"expected one invariant.violated event, got {len(inv_events)}"
    assert inv_events[0].data["name"] == "data_denial"
    assert inv_events[0].data["tool"] == "geo.find_poi"
    assert inv_events[0].data["records"] == 2

    # Final reply must be the GOOD retry, not the denial.
    assert "Dr Chan" in resp.text
    assert "couldn't find" not in resp.text.lower()
    assert "no data" not in resp.text.lower()


@pytest.mark.asyncio
@respx.mock
async def test_gate_rectifies_when_retry_still_violates_spatial_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v0.6.0 regression guard: gpt-oss-120b sometimes calls geo.find_poi
    without coords on Cantonese POI queries even after the corrective
    re-prompt. The orchestrator must escalate to deterministic
    rectification — drop the bare find_poi, inject geo.address_lookup, and
    let chain_rules glue the chain together. End state: address_lookup +
    find_poi(category=dentist, lat=..., lng=...) both fire OK."""
    # ALS upstream — returns Tsim Sha Tsui at 22.30, 114.17.
    respx.get("https://www.als.gov.hk/lookup").mock(
        return_value=httpx.Response(
            200,
            json={
                "SuggestedAddress": [
                    {
                        "Address": {
                            "PremisesAddress": {
                                "EngPremisesAddress": {
                                    "BuildingName": "Tsim Sha Tsui",
                                    "EngDistrict": {"DcDistrict": "Yau Tsim Mong"},
                                },
                                "ChiPremisesAddress": {"BuildingName": "尖沙咀"},
                                "GeospatialInformation": {
                                    "Latitude": "22.30",
                                    "Longitude": "114.17",
                                },
                            }
                        }
                    }
                ]
            },
        )
    )
    # Overpass upstream — returns one dentist near the resolved point.
    respx.post("https://overpass-api.de/api/interpreter").mock(
        return_value=httpx.Response(
            200,
            json={
                "elements": [
                    {
                        "type": "node",
                        "id": 1001,
                        "lat": 22.301,
                        "lon": 114.171,
                        "tags": {"name": "Dr Chan Dental Clinic", "amenity": "dentist"},
                    }
                ]
            },
        )
    )

    bare_find_poi = LLMReply(
        text="",
        tool_calls=[
            {
                "id": "call-bare-poi",
                "name": "geo.find_poi",
                "arguments": json.dumps({"category": "dentist"}),  # no coords
            }
        ],
        usage={},
        elapsed_ms=80,
    )
    synth = LLMReply(
        text="Dr Chan Dental Clinic 喺尖沙咀附近。",
        tool_calls=[],
        usage={},
        elapsed_ms=40,
    )
    from smcity import orchestrator as orch_module

    # Sequence: first call returns bare find_poi (gate fires + re-prompts),
    # retry returns the SAME bare shape (gate fires again → rectification
    # kicks in), then the synthesis call returns the prose reply.
    monkeypatch.setattr(
        orch_module, "chat", _scripted_chat([bare_find_poi, bare_find_poi, synth])
    )

    store = SessionStore(tmp_path / "sessions.sqlite3")
    orch = Orchestrator(store)
    req = TurnRequest(
        session_id="rectify-1",
        text="尖沙咀附近邊度有牙醫?",
        locale_override="yue",
    )

    events: list[Any] = []
    resp = await orch.handle_turn(req, emit=events.append)

    # Rectification event must have fired with the right kind.
    rect_events = [e for e in events if e.type == "gate.rectified"]
    assert len(rect_events) == 1, f"expected one gate.rectified event, got {rect_events}"
    assert rect_events[0].data["kind"] == "missing_spatial_scope"

    # The rectified batch should have run address_lookup, then chain_rules
    # auto-dispatched find_poi(category=dentist, lat=..., lng=...).
    tool_names = [t.name for t in resp.tool_trace]
    assert "geo.address_lookup" in tool_names
    assert "geo.find_poi" in tool_names
    poi_entries = [t for t in resp.tool_trace if t.name == "geo.find_poi"]
    assert poi_entries and poi_entries[0].args.get("category") == "dentist"
    for t in resp.tool_trace:
        assert t.status == "ok", f"expected ok, got {t.name}={t.status}"
