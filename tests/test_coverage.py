"""Coverage view tests.

Pin the catalog → registry reconciliation logic so we catch:
  * catalog drift (tool name changes without updating the JSON file)
  * silent dataset removal (someone deletes an entry by accident)
  * status totals matching the row count
"""

from __future__ import annotations

from smcity.coverage import compute_coverage
from smcity.tools import build_default_registry


def test_catalog_has_all_xlsx_datasets() -> None:
    """The Selected Smart City Data Maps xlsx has 35 datasets — the catalog
    must list every one. Drops mean someone deleted a row.
    """
    registry = build_default_registry()
    report = compute_coverage(set(registry.names()))
    assert report.summary.total_xlsx_datasets == 35
    ids = {d.id for d in report.datasets}
    # Spot-check the boundaries: smallest, largest, and a known partial.
    assert "S500" in ids
    assert "S549" in ids
    assert "S506" in ids  # ferry — known missing
    assert "S514" in ids  # convenience stores — known wired via OSM


def test_summary_totals_match_row_counts() -> None:
    registry = build_default_registry()
    report = compute_coverage(set(registry.names()))
    by_status = {"wired": 0, "partial": 0, "missing": 0}
    for d in report.datasets:
        by_status[d.status] += 1
    assert report.summary.wired == by_status["wired"]
    assert report.summary.partial == by_status["partial"]
    assert report.summary.missing == by_status["missing"]
    assert report.summary.wired + report.summary.partial + report.summary.missing == 35


def test_every_listed_tool_is_actually_registered() -> None:
    """Catalog-drift guard. Every tool name referenced in the catalog must
    exist in the live registry — otherwise the catalog is lying about
    coverage. If this fails: a tool was renamed without updating the
    catalog JSON.
    """
    registry = build_default_registry()
    registered = set(registry.names())
    report = compute_coverage(registered)
    missing_in_datasets: list[tuple[str, list[str]]] = []
    for d in report.datasets:
        if d.missing_tools:
            missing_in_datasets.append((d.id, d.missing_tools))
    missing_in_additional: list[tuple[str, list[str]]] = []
    for a in report.additional_integrations:
        if a.missing_tools:
            missing_in_additional.append((a.title, a.missing_tools))
    msg = (
        f"catalog drift detected — these tool names are in "
        f"data/coverage_catalog.json but NOT in the registry. "
        f"datasets: {missing_in_datasets}, "
        f"additional: {missing_in_additional}"
    )
    assert not missing_in_datasets and not missing_in_additional, msg


def test_wired_datasets_have_at_least_one_registered_tool() -> None:
    """If a dataset is marked 'wired', at least one of the tools that wires
    it must actually be in the registry — otherwise it's a lie.
    """
    registry = build_default_registry()
    report = compute_coverage(set(registry.names()))
    liars: list[str] = []
    for d in report.datasets:
        if d.status == "wired" and not d.any_tool_registered:
            liars.append(d.id)
    assert not liars, f"datasets marked 'wired' but no tool is registered: {liars}"


def test_missing_dataset_has_no_tools_listed() -> None:
    """S506 (ferry) is the canonical 'missing' entry — make sure it has an
    empty tools list (i.e. the catalog acknowledges nothing covers it).
    """
    registry = build_default_registry()
    report = compute_coverage(set(registry.names()))
    s506 = next(d for d in report.datasets if d.id == "S506")
    assert s506.status == "missing"
    assert s506.tools == []
    assert not s506.any_tool_registered
