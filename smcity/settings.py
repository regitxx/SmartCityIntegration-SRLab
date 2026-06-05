"""Runtime configuration — all env-driven, nothing hardcoded."""

from __future__ import annotations

from functools import cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    llm_base_url: str = Field(
        default="http://earnests-mac-studio.taila366aa.ts.net:1234/v1",
        description="OpenAI-compatible base URL (LM Studio on the Mac Studio).",
    )
    llm_model: str = Field(default="openai/gpt-oss-120b")
    llm_timeout_s: float = Field(default=30.0, ge=1.0, le=300.0)

    bind_host: str = Field(default="127.0.0.1")
    bind_port: int = Field(default=8080, ge=1, le=65535)
    log_level: str = Field(default="INFO")

    enable_tailscale_only: bool = Field(default=True)
    session_ttl_hours: int = Field(default=24, ge=1, le=24 * 30)
    pii_redact_at_ingress: bool = Field(default=True)

    # WebSocket origin allow-list. Comma-separated host[:port] or scheme://host
    # entries — empty string (default) means accept same-origin requests only.
    # Use `*` to disable the check entirely (NOT recommended off-tailnet).
    ws_allowed_origins: str = Field(default="")

    # Per-session rate limit (token-bucket): refill `rate_per_min` tokens per
    # minute, burst up to `rate_burst`. Each turn costs one token. Disabled
    # when either value is 0.
    rate_per_min: int = Field(default=30, ge=0, le=600)
    rate_burst: int = Field(default=10, ge=0, le=120)

    # --- local POI mirror (geo.find_poi) ---------------------------------
    # SQLite + R*Tree mirror of OSM POIs, refreshed nightly from Overpass.
    # `find_poi` queries this first; live `overpass-api.de` is a fallback.
    poi_store_enabled: bool = Field(
        default=True,
        description="Query the local POI mirror before live Overpass.",
    )
    poi_overpass_fallback: bool = Field(
        default=True,
        description=(
            "On a mirror miss (category not yet refreshed) or store error, fall "
            "back to live Overpass. The A/B switch: set false to measure the "
            "mirror in isolation."
        ),
    )
    poi_store_path: str = Field(
        default="state/poi.sqlite",
        description=(
            "Path to the POI mirror DB. In the docker deploy this is set to "
            "/app/state/poi.sqlite (the shared named volume) so both replicas "
            "read the same mirror."
        ),
    )
    poi_refresh_enabled: bool = Field(
        default=True,
        description="Run the in-process nightly refresh loop on startup.",
    )
    poi_refresh_interval_hours: float = Field(
        default=24.0,
        ge=0.25,
        le=24 * 7,
        description="Hours between mirror refreshes.",
    )
    poi_refresh_throttle_s: float = Field(
        default=2.0,
        ge=0.0,
        le=30.0,
        description="Pause between per-category Overpass queries during refresh.",
    )


@cache
def get_settings() -> Settings:
    """Module-level memoised settings getter."""
    return Settings()
