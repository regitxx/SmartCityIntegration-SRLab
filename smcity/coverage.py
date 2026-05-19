"""Coverage view — what data the agent actually consumes.

Loads ``data/coverage_catalog.json`` (the hand-curated mapping of each
dataset in ``3 - Selected Smart City Data Maps.xlsx`` to the agent tools
that consume it) and enriches each entry with **live** registry state —
i.e. for every tool name listed in the catalog, we report whether that
tool is currently registered in the running agent.

This is what the boss asked for: "show coverage of the data.gov.hk
API or from the excel. I want to see what is added and what isn't added."

The catalog is the source of truth for INTENT (we plan to wire S506, it's
just not done yet). The registry is the source of truth for STATE (this
exact build has these tool names registered). Reconciliation between the
two surfaces both: the visible status and any drift if a tool gets
renamed without updating the catalog.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "coverage_catalog.json"

# Latest coverage-suite summary JSON, written by
# ``smcity_fuzz.coverage_report`` and surfaced in the /coverage response
# under ``test_summary`` so the /data page can show per-dataset test
# health side-by-side with the static catalog status.
#
# Lives in /app/state (volume-backed) — NOT /app/data (image-baked) — so
# a fresh test run can update it without rebuilding the container image.
# Falls back to the dev path under repo/data/ for local runs.
_TEST_SUMMARY_PATH_PROD = Path("/app/state/coverage_test_summary.json")
_TEST_SUMMARY_PATH_DEV = (
    Path(__file__).resolve().parent.parent / "data" / "coverage_test_summary.json"
)

CoverageStatus = Literal["wired", "partial", "missing"]


class DatasetCoverage(BaseModel):
    id: str
    title: str
    category: str
    source: str
    url: str | None = None
    format: str | None = None
    status: CoverageStatus
    tools: list[str]
    osm_category: str | None = None
    notes: str | None = None
    # Filled in at runtime by `compute_coverage`. `True` = at least one
    # listed tool is currently registered; `False` = none are. Helps spot
    # catalog drift (catalog says tool X covers it but X isn't actually
    # in the registry — usually a rename).
    any_tool_registered: bool = False
    missing_tools: list[str] = []


class AdditionalIntegration(BaseModel):
    title: str
    category: str
    source: str
    url: str | None = None
    tools: list[str]
    notes: str | None = None
    any_tool_registered: bool = False
    missing_tools: list[str] = []


class CoverageSummary(BaseModel):
    total_xlsx_datasets: int
    wired: int
    partial: int
    missing: int
    registered_tool_count: int
    xlsx_source: str
    catalog_version: str


class TestSummaryDataset(BaseModel):
    dataset_id: str
    total: int
    expected_tool_hit_rate: float
    avg_elapsed_ms: int
    buckets: dict[str, int]
    top_tools_fired: list[Any] = []  # list[ tuple[str, int] ] roundtripped as JSON


class TestSummary(BaseModel):
    generated_at: str
    datasets: list[TestSummaryDataset]


class CoverageReport(BaseModel):
    summary: CoverageSummary
    datasets: list[DatasetCoverage]
    additional_integrations: list[AdditionalIntegration]
    # Optional — only present when a coverage suite has been run and the
    # summary JSON has been copied into data/coverage_test_summary.json.
    test_summary: TestSummary | None = None


@cache
def _load_raw_catalog() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
    return data


def compute_coverage(registered_tool_names: set[str]) -> CoverageReport:
    """Build the coverage view from the catalog file + the live registry.

    Args:
        registered_tool_names: the result of ``registry.names()`` —
            the exact set of tool names that this build has.

    Returns:
        A `CoverageReport` ready to JSON-serialise for the /coverage
        endpoint or render in the UI.
    """
    raw = _load_raw_catalog()
    datasets: list[DatasetCoverage] = []
    n_wired = n_partial = n_missing = 0

    for entry in raw["datasets"]:
        tool_list: list[str] = list(entry.get("tools") or [])
        missing = [t for t in tool_list if t not in registered_tool_names]
        any_registered = any(t in registered_tool_names for t in tool_list)
        ds = DatasetCoverage(
            id=entry["id"],
            title=entry["title"],
            category=entry["category"],
            source=entry["source"],
            url=entry.get("url"),
            format=entry.get("format"),
            status=entry["status"],
            tools=tool_list,
            osm_category=entry.get("osm_category"),
            notes=entry.get("notes"),
            any_tool_registered=any_registered,
            missing_tools=missing,
        )
        datasets.append(ds)
        if ds.status == "wired":
            n_wired += 1
        elif ds.status == "partial":
            n_partial += 1
        else:
            n_missing += 1

    additional: list[AdditionalIntegration] = []
    for entry in raw.get("additional_integrations", []):
        tool_list = list(entry.get("tools") or [])
        missing = [t for t in tool_list if t not in registered_tool_names]
        any_registered = any(t in registered_tool_names for t in tool_list)
        additional.append(
            AdditionalIntegration(
                title=entry["title"],
                category=entry["category"],
                source=entry["source"],
                url=entry.get("url"),
                tools=tool_list,
                notes=entry.get("notes"),
                any_tool_registered=any_registered,
                missing_tools=missing,
            )
        )

    summary = CoverageSummary(
        total_xlsx_datasets=len(datasets),
        wired=n_wired,
        partial=n_partial,
        missing=n_missing,
        registered_tool_count=len(registered_tool_names),
        xlsx_source=raw.get("source_xlsx", ""),
        catalog_version=raw.get("version", ""),
    )

    test_summary = _load_test_summary()
    return CoverageReport(
        summary=summary,
        datasets=datasets,
        additional_integrations=additional,
        test_summary=test_summary,
    )


def _load_test_summary() -> TestSummary | None:
    """Read the latest coverage-suite summary if it exists on disk.

    The summary file is written by ``smcity_fuzz coverage report
    --out-json …/coverage_test_summary.json`` and read live each request
    so a new test run is reflected immediately — no agent restart.
    Looks in /app/state first (production volume-backed path), then in
    the repo's data/ directory as a dev fallback.
    """
    for path in (_TEST_SUMMARY_PATH_PROD, _TEST_SUMMARY_PATH_DEV):
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return TestSummary.model_validate(raw)
        except (json.JSONDecodeError, ValueError):
            continue
    return None


__all__ = [
    "AdditionalIntegration",
    "CoverageReport",
    "CoverageStatus",
    "CoverageSummary",
    "DatasetCoverage",
    "compute_coverage",
]
