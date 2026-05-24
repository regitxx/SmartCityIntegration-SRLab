"""`just live-smoke` — live integration test of the v0.5.1+v0.5.2 engines.

Hits the real LM Studio via the orchestrator (no FastAPI layer between).
For each query, prints:

- the reply text (truncated to 300 chars)
- the tool trace (call order + status)
- whether the chain/gate/invariant engines fired this turn
- end-to-end latency

There's no pass/fail — read the output. This is behavioral smoke, not an
assertion suite. We're confirming each engine fires on a representative
query against the real LLM, and that the v0.5.2 description trim has
restored tool-calling on transport datasets.

Run with: `python scripts/live_smoke.py`
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from smcity.orchestrator import Orchestrator, TurnEvent
from smcity.schemas import TurnRequest
from smcity.session import SessionStore
from smcity.settings import get_settings

# Each query is (session_id, text, locale_override, why_we_test_this).
# Order matters slightly: we hit the transit case first because if the
# v0.5.0 prompt-bloat regression is still alive we'll see it fail there
# before wasting time on the POI / planner cases.
QUERIES: list[tuple[str, str, str | None, str]] = [
    (
        "smoke-mtr-en",
        "next train at Central",
        "en",
        "v0.5.2 regression check: transport.get_mtr_next_trains must fire",
    ),
    (
        "smoke-poi-en",
        "where's the nearest 7-eleven near Tsim Sha Tsui?",
        "en",
        "chain_rules AutoDispatch (EN): address_lookup -> geo.find_convenience_store",
    ),
    (
        "smoke-poi-yue",
        "尖沙咀附近邊度有牙醫?",
        "yue",
        "chain_rules AutoDispatch (yue): address_lookup -> geo.find_dentist",
    ),
    (
        "smoke-plan-default",
        "how do I get from Central to Sha Tin?",
        "en",
        "Scope tags: should pick plan_journey [DEFAULT: any_mode_journey]",
    ),
    (
        "smoke-plan-mtr",
        "MTR from Central to Sha Tin",
        "en",
        "Scope tags: should pick plan_simple_route [SPECIALIZED: mtr_only] when MTR explicit",
    ),
    (
        "smoke-ask-bait",
        "find me a place to go",
        "en",
        "ASK_USER_ONLY_GATE: vague query; if LLM leads with ask_user, gate redirects",
    ),
]


def _engine_events(events: list[TurnEvent]) -> dict[str, list[dict]]:
    """Group telemetry events fired by the three engines for this turn."""
    interesting = ("chain.fired", "gate.violated", "invariant.violated")
    out: dict[str, list[dict]] = {}
    for e in events:
        if e.type in interesting:
            out.setdefault(e.type, []).append(e.data)
    return out


async def _run_one(orch: Orchestrator, sid: str, text: str, locale: str | None) -> None:
    events: list[TurnEvent] = []
    req = TurnRequest(session_id=sid, text=text, locale_override=locale)
    resp = await orch.handle_turn(req, emit=events.append)

    tools = " -> ".join(f"{t.name}({t.status})" for t in resp.tool_trace) or "(none)"
    engines = _engine_events(events)

    reply = resp.text.replace("\n", " ")
    print(f"  reply:   {reply[:300]}")
    if len(reply) > 300:
        print(f"           ... (+{len(reply) - 300} more chars)")
    print(f"  tools:   {tools}")
    print(f"  engines: {engines or '(none fired)'}")
    print(f"  latency: {resp.elapsed_ms} ms")


async def main() -> int:
    s = get_settings()
    print(f"LM Studio: {s.llm_base_url}")
    print(f"Model:     {s.llm_model}")
    print()

    db = Path("/tmp/smcity_live_smoke.sqlite3")
    store = SessionStore(db)
    orch = Orchestrator(store)

    fail_count = 0
    for sid, text, locale, why in QUERIES:
        print(f"=== {sid} ===")
        print(f"  why:     {why}")
        print(f"  query:   {text!r} (locale={locale})")
        try:
            await _run_one(orch, sid, text, locale)
        except Exception as err:
            print(f"  ERROR:   {type(err).__name__}: {err}")
            fail_count += 1
        print()

    return 1 if fail_count else 0


if __name__ == "__main__":
    # Fresh session store each run — smoke tests should not see history.
    _db = Path("/tmp/smcity_live_smoke.sqlite3")
    if _db.exists():
        _db.unlink()
    sys.exit(asyncio.run(main()))
