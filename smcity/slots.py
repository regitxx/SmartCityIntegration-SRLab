"""Slot-filling state machine — tracks what the orchestrator knows so far.

Slot values are persisted per-session via smcity.session. The clarify-vs-guess
policy lives here so it's testable without the LLM in the loop.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from smcity.langrouter.detect import LangDetection
from smcity.schemas import UserLocation

TransportMode = Literal["mtr", "bus", "minibus", "tram", "ferry", "taxi", "walk", "drive", "any"]


class LocationSlot(BaseModel):
    raw: str
    resolved_lat: float | None = None
    resolved_lng: float | None = None
    resolved_name_en: str | None = None
    resolved_name_tc: str | None = None


class Locale(BaseModel):
    primary_lang: str = "auto"
    script: str = "Other"
    tts_locale: str = "en-US"
    source: Literal["detected", "forced", "carried", "auto"] = "auto"
    confidence: float = 0.0

    @classmethod
    def from_detection(cls, d: LangDetection, *, forced: bool = False) -> Locale:
        return cls(
            primary_lang=d.primary_lang,
            script=d.script,
            tts_locale=d.tts_locale,
            source="forced" if forced else "detected",
            confidence=d.confidence,
        )


class HistoryMsg(BaseModel):
    """One conversational turn as persisted between requests."""

    role: Literal["user", "assistant"]
    content: str
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# Keep this short — context tokens add to every turn's LLM bill and LM Studio's
# KV cache churns when the prefix grows. 10 pairs = 20 messages = usually
# enough for a multi-turn clarification flow without blowing the window.
_MAX_HISTORY = 20


class SessionSlots(BaseModel):
    session_id: str
    origin: LocationSlot | None = None
    destination: LocationSlot | None = None
    mode: TransportMode | None = None
    depart_time: datetime | None = None  # aware, HKT
    venue_type: str | None = None  # e.g. "basketball_court", "library"
    accessibility: list[str] = Field(default_factory=list)
    horizon: timedelta | None = None
    locale: Locale = Field(default_factory=Locale)
    user_location: UserLocation | None = None
    history: list[HistoryMsg] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def needs_origin_destination(self) -> bool:
        return self.origin is None or self.destination is None

    def needs_mode(self) -> bool:
        return self.mode is None

    def touch(self) -> None:
        self.updated_at = datetime.now(UTC)

    def append_turn(self, user_text: str, assistant_text: str) -> None:
        """Append one (user, assistant) pair and trim to the history cap."""
        self.history.append(HistoryMsg(role="user", content=user_text))
        self.history.append(HistoryMsg(role="assistant", content=assistant_text))
        if len(self.history) > _MAX_HISTORY:
            # Drop oldest first. Keep pairs aligned (an even cut).
            overflow = len(self.history) - _MAX_HISTORY
            if overflow % 2 == 1:
                overflow += 1
            self.history = self.history[overflow:]


# --- clarify-vs-guess policy ---------------------------------------------


class ClarifyPolicy(BaseModel):
    """Encoded rules for when to ask vs when to best-guess.

    Deliberately a table, not a prompt — so it's unit-testable.
    """

    def next_question(self, slots: SessionSlots) -> str | None:
        """Return the *kind* of clarification needed, or None if the
        orchestrator should proceed.

        Kinds:
        - "origin"      : we don't know where the user is starting from
        - "destination" : we don't know where the user is going
        - "mode"        : transport mode is ambiguous / unstated
        - "venue_type"  : vague "find me a facility" with no type
        """
        has_origin = slots.origin is not None or slots.user_location is not None
        has_destination = slots.destination is not None or slots.venue_type is not None
        if not has_origin:
            return "origin"
        if not has_destination:
            return "destination"
        if slots.mode is None:
            return "mode"
        return None
