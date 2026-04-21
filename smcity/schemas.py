"""Public pydantic schemas for the agent service."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

LocaleCode = str  # ISO 639-1/3 or "auto"; validated against a known set at the router edge.


class UserLocation(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    source: Literal["user_typed", "platform_push", "estimated"] = "user_typed"


class TurnRequest(BaseModel):
    """Body for `POST /turn`."""

    session_id: str = Field(min_length=1, max_length=128)
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


class ToolTraceEntry(BaseModel):
    index: int
    name: str
    args: dict[str, object] = Field(default_factory=dict)
    status: Literal["ok", "error", "timeout", "rate_limited", "skipped"]
    latency_ms: int
    result_summary: str | None = None


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
