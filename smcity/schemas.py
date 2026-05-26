"""Public pydantic schemas for the agent service."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

LocaleCode = str  # ISO 639-1/3 or "auto"; validated against a known set at the router edge.

# Session IDs travel on the WebSocket URL path; keep them to an opaque ASCII
# token so we can't smuggle control chars, slashes, or UTF-8 gremlins into the
# SQLite layer. Mirrors smcity.session._SESSION_ID_RE.
_SESSION_ID_PATTERN = r"^[A-Za-z0-9_.-]{1,64}$"


class UserLocation(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    source: Literal["user_typed", "platform_push", "estimated"] = "user_typed"


class TurnRequest(BaseModel):
    """Body for `POST /turn`."""

    session_id: str = Field(pattern=_SESSION_ID_PATTERN)
    text: str = Field(min_length=1, max_length=4000)
    locale_override: LocaleCode | None = Field(
        default=None,
        description="If set, bypasses detection and forces this locale for input + output.",
    )
    user_location: UserLocation | None = Field(default=None)


class Citation(BaseModel):
    tool: str
    upstream: str
    fetched_at: datetime
    upstream_langs: list[str] = Field(default_factory=list)
    translation_applied: bool = False
    # Optional sub-type within `tool` — e.g. for `geo.find_poi` this carries
    # the category slug ("dentist", "convenience_store", …) so the footer
    # can render `find_poi/dentist` instead of bare `find_poi`. None when
    # the tool has no meaningful sub-type.
    discriminator: str | None = None


class ToolTraceEntry(BaseModel):
    index: int
    name: str
    args: dict[str, object] = Field(default_factory=dict)
    status: Literal["ok", "error", "timeout", "rate_limited", "skipped"]
    latency_ms: int
    result_summary: str | None = None
    # Full normalised tool result — populated on status=="ok". Consumers
    # (fuzz judge, UI debug panel, post-hoc grading) use this to verify
    # the agent's claims against what the tool actually returned; the
    # human-readable `result_summary` is kept separately for chat-UI tooltips.
    result: dict[str, object] | None = None


class LanguageCoverage(BaseModel):
    source: Literal["detected", "forced"]
    primary_lang: LocaleCode
    upstream_langs_available: list[str] = Field(default_factory=list)
    translation_applied: bool = False


class TurnResponse(BaseModel):
    """Body of `POST /turn` reply."""

    session_id: str
    text: str
    lang: LanguageCoverage
    citations: list[Citation] = Field(default_factory=list)
    tool_trace: list[ToolTraceEntry] = Field(default_factory=list)
    followups: list[str] = Field(default_factory=list)
    elapsed_ms: int


class Health(BaseModel):
    status: Literal["ok", "degraded", "down"]
    llm_reachable: bool
    llm_model: str
    version: str
