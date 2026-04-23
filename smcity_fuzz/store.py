"""Append-only JSONL store for fuzzer runs.

One file at `settings.runs_path` (default `logs/fuzz_runs.jsonl`). One
line per turn; survives crashes because we flush+fsync after every row.
Grep-friendly and reproducible.

Each row is a self-describing object keyed by `run_id` (a campaign ID)
+ `ts` (ISO timestamp). Post-hoc analysis filters on those.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from smcity_fuzz.judge import JudgeVerdict
from smcity_fuzz.settings import FuzzSettings, get_fuzz_settings


class FuzzRow(BaseModel):
    run_id: str
    ts: str
    persona: str
    language: str
    topic: str
    question: str
    reply: str | None = None
    tool_trace: list[dict[str, Any]] = Field(default_factory=list)
    elapsed_ms: int | None = None
    # Transport: "http" (POST /turn) or "ws" (streaming /ws/{session_id}).
    # Populated by the runner; default http for backward compat with
    # existing JSONL rows.
    transport: str = "http"
    # Streaming-mode timing fields; only populated when transport == "ws".
    ttft_ms: int | None = None  # time-to-first-token from turn.start → first turn.token
    token_count: int | None = None  # how many incremental tokens the UI would render
    judge: JudgeVerdict | None = None
    errors: list[str] = Field(default_factory=list)

    @property
    def failed(self) -> bool:
        if self.errors:
            return True
        if self.judge is None:
            return True  # couldn't score ⇒ can't call it passing
        return self.judge.failed


def iso_now() -> str:
    return datetime.now(UTC).isoformat()


def append_row(row: FuzzRow, *, settings: FuzzSettings | None = None) -> None:
    s = settings or get_fuzz_settings()
    path = Path(s.runs_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = row.model_dump_json(exclude_none=False) + "\n"
    # Open in append + binary so we can fsync the bytes deterministically.
    with path.open("ab") as fh:
        fh.write(line.encode("utf-8"))
        fh.flush()
        os.fsync(fh.fileno())


def read_rows(*, settings: FuzzSettings | None = None) -> list[FuzzRow]:
    """Load every row in the JSONL file (small enough to hold in RAM)."""
    s = settings or get_fuzz_settings()
    path = Path(s.runs_path)
    if not path.exists():
        return []
    rows: list[FuzzRow] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            rows.append(FuzzRow.model_validate_json(line))
        except Exception:  # noqa: S112 — skip corrupt lines instead of crashing the read
            continue
    return rows
