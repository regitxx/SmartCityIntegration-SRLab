"""Runtime configuration for the fuzzer — env-driven.

The fuzzer reuses the production LM Studio endpoint (same Tailscale URL
as `smcity.settings.llm_base_url`) but points at a smaller model for
synth + judge so the 120 B production brain stays responsive.
"""

from __future__ import annotations

from functools import cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class FuzzSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        env_prefix="FUZZER_",
        extra="ignore",
    )

    # LM Studio endpoint + model for synth + judge. Defaults mirror the
    # production agent's endpoint; swap `model` to the smaller 20 B brain
    # so the fuzzer doesn't starve the production 120 B on shared slots.
    base_url: str = Field(
        default="http://earnests-mac-studio.taila366aa.ts.net:1234/v1",
    )
    model: str = Field(default="openai/gpt-oss-20b")
    timeout_s: float = Field(default=30.0, ge=1.0, le=300.0)

    # Where to reach the smcity agent under test.
    agent_url: str = Field(default="http://127.0.0.1:8080")
    agent_timeout_s: float = Field(default=60.0, ge=1.0, le=300.0)

    # Where to append the structured run log. JSONL — one row per turn.
    runs_path: str = Field(default="logs/fuzz_runs.jsonl")

    # How many concurrent turns may be in flight. Keep low by default so
    # we don't saturate LM Studio slots or create unrealistic contention.
    concurrency: int = Field(default=2, ge=1, le=16)


@cache
def get_fuzz_settings() -> FuzzSettings:
    return FuzzSettings()
