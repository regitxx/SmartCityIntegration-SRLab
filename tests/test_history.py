"""Tests for session-level conversation history."""

from __future__ import annotations

from smcity.slots import SessionSlots


def test_append_turn_persists_both_roles() -> None:
    slots = SessionSlots(session_id="h1")
    slots.append_turn("hi", "Hi back")
    assert [(m.role, m.content) for m in slots.history] == [
        ("user", "hi"),
        ("assistant", "Hi back"),
    ]


def test_append_turn_trims_to_cap() -> None:
    slots = SessionSlots(session_id="h2")
    # 15 turns = 30 entries; cap is 20 → 10 newest pairs retained.
    for i in range(15):
        slots.append_turn(f"q{i}", f"a{i}")
    assert len(slots.history) == 20
    # First-kept pair must be q5/a5.
    assert slots.history[0].content == "q5"
    assert slots.history[1].content == "a5"
    assert slots.history[-2].content == "q14"
    assert slots.history[-1].content == "a14"


def test_history_roundtrips_through_pydantic_json() -> None:
    slots = SessionSlots(session_id="h3")
    slots.append_turn("how do I get from North Point to basketball court", "which mode?")
    raw = slots.model_dump_json()
    restored = SessionSlots.model_validate_json(raw)
    assert [(m.role, m.content) for m in restored.history] == [
        ("user", "how do I get from North Point to basketball court"),
        ("assistant", "which mode?"),
    ]
