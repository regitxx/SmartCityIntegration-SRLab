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


class CoverageReport(BaseModel):
    summary: CoverageSummary
    datasets: list[DatasetCoverage]
    additional_integrations: list[AdditionalIntegration]


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

    return CoverageReport(
        summary=summary,
        datasets=datasets,
        additional_integrations=additional,
    )


__all__ = [
    "AdditionalIntegration",
    "CoverageReport",
    "CoverageStatus",
    "CoverageSummary",
    "DatasetCoverage",
    "compute_coverage",
]
