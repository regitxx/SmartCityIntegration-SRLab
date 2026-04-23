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


@cache
def get_settings() -> Settings:
    """Module-level memoised settings getter."""
    return Settings()
