"""SessionSlots + ClarifyPolicy unit tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from smcity.langrouter.detect import LangDetection
from smcity.session import SessionStore, redact_pii
from smcity.slots import ClarifyPolicy, Locale, LocationSlot, SessionSlots


def test_clarify_policy_asks_for_origin_first() -> None:
    policy = ClarifyPolicy()
    slots = SessionSlots(session_id="s")
    assert policy.next_question(slots) == "origin"
    slots.origin = LocationSlot(raw="Sheung Wan")
    assert policy.next_question(slots) == "destination"
    slots.destination = LocationSlot(raw="Sha Tin")
    assert policy.next_question(slots) == "mode"
    slots.mode = "mtr"
    assert policy.next_question(slots) is None


def test_clarify_policy_accepts_user_location_as_origin() -> None:
    from smcity.schemas import UserLocation

    slots = SessionSlots(session_id="s", user_location=UserLocation(lat=22.28, lng=114.15))
    policy = ClarifyPolicy()
    assert policy.next_question(slots) == "destination"


def test_clarify_policy_treats_venue_type_as_destination() -> None:
    slots = SessionSlots(session_id="s", venue_type="basketball_court")
    slots.origin = LocationSlot(raw="here")
    policy = ClarifyPolicy()
    # destination is implied by venue_type; next gap is mode
    assert policy.next_question(slots) == "mode"


def test_pii_redaction_masks_phone_and_hkid() -> None:
    assert redact_pii("call me 9123 4567") == "call me [PHONE]"
    assert redact_pii("+852 2345 6789") == "[PHONE]"
    assert "[HKID]" in redact_pii("my hkid is A123456(7)")


def test_locale_from_detection_preserves_tts_locale() -> None:
    d = LangDetection(primary_lang="yue", script="Hant", tts_locale="yue-HK", confidence=0.92)
    loc = Locale.from_detection(d)
    assert loc.tts_locale == "yue-HK"
    assert loc.source == "detected"


@pytest.mark.asyncio
async def test_session_store_roundtrip(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite3")
    slots = SessionSlots(session_id="abc", origin=LocationSlot(raw="Sheung Wan"))
    await store.save(slots)
    loaded = await store.load("abc")
    assert loaded.session_id == "abc"
    assert loaded.origin is not None
    assert loaded.origin.raw == "Sheung Wan"
    await store.forget("abc")
    cleared = await store.load("abc")
    assert cleared.origin is None
