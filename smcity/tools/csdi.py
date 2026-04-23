"""CSDI ArcGIS FeatureServer client + generic query tool.

Hong Kong's Common Spatial Data Infrastructure (CSDI) and most HKSAR agencies
publish open spatial data through ArcGIS REST FeatureServer services. The
endpoints follow the canonical Esri pattern:

    {base}/rest/services/{service}/FeatureServer/{layer}/query

This module provides:

- `query_feature_server(...)` — low-level async client that transparently
  follows ArcGIS pagination (`exceededTransferLimit: true`) and normalises
  responses into `ArcGisFeature` records with plain lat/lng plus an
  attributes dict.
- `CSDI_DATASETS` — a small registry mapping short IDs to
  `CSDIDataset` records (URL + field list + coord-system flags). This is
  populated as we verify each endpoint through `docs/research/06_csdi_endpoints.md`.
- `CSDI_QUERY_TOOL` — an agent-facing tool spec that exposes the above to
  the LLM as `csdi.query_features(dataset, where, bbox, limit)`. Args are
  validated against `CSDI_DATASETS` so the model can only query known
  services — SSRF-safe by construction.

Coord system note: ArcGIS services honour `outSR=4326` to return WGS84
decimal degrees for query results, even when the underlying data is stored
in HK1980 Grid (EPSG:2326). We always request WGS84 from the server so the
client never has to transform coordinates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, Field

from smcity.tools.registry import ToolContext, ToolSpec, ToolUpstreamError

# --- dataset registry ----------------------------------------------------

# Known dataset IDs — extended as each endpoint is verified. We keep this as
# a plain str rather than a Literal so a typo surfaces as a runtime error
# from the registry lookup (with a helpful "allowed: ..." list) rather than
# a stringly-typed mypy false negative that would force us to edit the type
# each time a dataset lands.
CSDIDatasetId = str


@dataclass(slots=True, frozen=True)
class CSDIDataset:
    """A verified CSDI FeatureServer endpoint plus its schema essentials."""

    id: CSDIDatasetId
    title_en: str
    url: str  # full .../FeatureServer/{layer} URL (no trailing /query)
    name_field_en: str
    name_field_tc: str
    # Optional: a whitelist of attributes the agent is allowed to ask for.
    # `None` means "return everything the endpoint publishes".
    allowed_out_fields: tuple[str, ...] | None = None
    # Max records the endpoint returns per page (pagination cap).
    max_record_count: int = 1000
    # Human description written back to the LLM tool schema.
    description: str = ""


# Datasets are registered as each one is verified by the research agent.
# Empty registry is valid — the tool surfaces a helpful error until at
# least one entry lands.
CSDI_DATASETS: dict[CSDIDatasetId, CSDIDataset] = {}


def register_dataset(ds: CSDIDataset) -> None:
    """Append a verified CSDI dataset to the global registry."""
    CSDI_DATASETS[ds.id] = ds


# --- client --------------------------------------------------------------


class ArcGisFeature(BaseModel):
    """A single normalised feature returned from an ArcGIS query."""

    attributes: dict[str, Any] = Field(default_factory=dict)
    lat: float | None = None
    lng: float | None = None


class ArcGisQueryResult(BaseModel):
    """Normalised result of a paginated ArcGIS FeatureServer query."""

    features: list[ArcGisFeature]
    total: int
    truncated: bool = False
    spatial_ref: int = 4326


def _parse_feature(raw: dict[str, Any]) -> ArcGisFeature:
    attrs = raw.get("attributes") or {}
    geom = raw.get("geometry") or {}
    lat: float | None = None
    lng: float | None = None
    # Point geometry is most common; for polygons/lines we drop the geometry
    # and rely on centroid attributes if the dataset ships them.
    if "y" in geom and "x" in geom:
        lat = float(geom["y"])
        lng = float(geom["x"])
    return ArcGisFeature(attributes=attrs, lat=lat, lng=lng)


async def query_feature_server(
    url: str,
    *,
    where: str = "1=1",
    out_fields: str = "*",
    bbox: tuple[float, float, float, float] | None = None,
    out_sr: int = 4326,
    limit: int | None = 1000,
    page_size: int = 1000,
    timeout_s: float = 8.0,
    http_client: httpx.AsyncClient | None = None,
) -> ArcGisQueryResult:
    """Query an ArcGIS FeatureServer layer, following pagination.

    `url` is the layer URL without `/query` (we append it here). `bbox` is
    `(min_lng, min_lat, max_lng, max_lat)` in WGS84 — translated to the
    ArcGIS `geometry` + `geometryType=esriGeometryEnvelope` parameters.
    `limit` caps how many records we return to the caller; pagination still
    happens under the hood in `page_size` batches.
    """
    base_params: dict[str, str] = {
        "where": where,
        "outFields": out_fields,
        "f": "json",
        "outSR": str(out_sr),
        "returnGeometry": "true",
        "resultRecordCount": str(page_size),
    }
    if bbox is not None:
        min_lng, min_lat, max_lng, max_lat = bbox
        base_params["geometry"] = f"{min_lng},{min_lat},{max_lng},{max_lat}"
        base_params["geometryType"] = "esriGeometryEnvelope"
        base_params["inSR"] = "4326"
        base_params["spatialRel"] = "esriSpatialRelIntersects"

    collected: list[ArcGisFeature] = []
    offset = 0
    truncated = False

    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=timeout_s)
    try:
        while True:
            params = {**base_params, "resultOffset": str(offset)}
            try:
                r = await client.get(f"{url.rstrip('/')}/query", params=params)
                r.raise_for_status()
                payload = r.json()
            except httpx.HTTPError as err:
                raise ToolUpstreamError(f"CSDI query failed: {err}") from err
            except ValueError as err:
                raise ToolUpstreamError(f"CSDI returned non-JSON: {err}") from err

            if isinstance(payload.get("error"), dict):
                detail = payload["error"].get("message", "unknown error")
                raise ToolUpstreamError(f"CSDI error: {detail}")

            page = [_parse_feature(f) for f in (payload.get("features") or [])]
            collected.extend(page)

            exceeded = bool(payload.get("exceededTransferLimit"))
            if limit is not None and len(collected) >= limit:
                collected = collected[:limit]
                truncated = truncated or exceeded
                break
            if not exceeded or not page:
                break
            offset += len(page)
    finally:
        if owns_client:
            await client.aclose()

    return ArcGisQueryResult(
        features=collected,
        total=len(collected),
        truncated=truncated,
        spatial_ref=out_sr,
    )


# --- agent-facing tool ---------------------------------------------------


class CSDIQueryArgs(BaseModel):
    dataset: str = Field(
        description="CSDI dataset ID (must be one of the registered IDs — "
        "call with an unknown id to get the list).",
    )
    where: str = Field(
        default="1=1",
        description="ArcGIS SQL WHERE clause, e.g. District='Central and Western'. "
        "Use '1=1' to return all rows; quote string literals with single quotes.",
    )
    bbox: tuple[float, float, float, float] | None = Field(
        default=None,
        description="Optional spatial filter (min_lng, min_lat, max_lng, max_lat) "
        "in WGS84 decimal degrees.",
    )
    limit: int = Field(default=100, ge=1, le=2000)


class CSDIQueryResult(BaseModel):
    dataset: str
    total: int
    truncated: bool
    features: list[dict[str, Any]]


async def _csdi_query_handler(args: CSDIQueryArgs, _ctx: ToolContext) -> CSDIQueryResult:
    if not CSDI_DATASETS:
        raise ToolUpstreamError(
            "CSDI dataset registry is empty; populate smcity.tools.csdi.CSDI_DATASETS"
        )
    ds = CSDI_DATASETS.get(args.dataset)
    if ds is None:
        allowed = ", ".join(sorted(CSDI_DATASETS)) or "(none yet)"
        raise ToolUpstreamError(f"unknown CSDI dataset {args.dataset!r}; allowed: {allowed}")
    out_fields = ",".join(ds.allowed_out_fields) if ds.allowed_out_fields else "*"
    result = await query_feature_server(
        ds.url,
        where=args.where,
        out_fields=out_fields,
        bbox=args.bbox,
        limit=args.limit,
        page_size=ds.max_record_count,
    )
    features = [
        {
            "lat": f.lat,
            "lng": f.lng,
            "name_en": f.attributes.get(ds.name_field_en),
            "name_tc": f.attributes.get(ds.name_field_tc),
            "attributes": f.attributes,
        }
        for f in result.features
    ]
    return CSDIQueryResult(
        dataset=args.dataset,
        total=result.total,
        truncated=result.truncated,
        features=features,
    )


def _build_description() -> str:
    """Describe the tool in terms of the currently-registered datasets."""
    if not CSDI_DATASETS:
        return (
            "Query a Hong Kong CSDI ArcGIS FeatureServer dataset by ID. No datasets registered yet."
        )
    rows = [f"- {ds.id}: {ds.title_en} — {ds.description}" for ds in CSDI_DATASETS.values()]
    return (
        "Query a Hong Kong CSDI ArcGIS FeatureServer dataset by ID. Use for "
        "facility-catalog style lookups (courts, pools, estates, etc.). "
        "Returns WGS84 lat/lng + bilingual name + raw attributes.\n"
        "Registered datasets:\n" + "\n".join(rows)
    )


CSDI_QUERY_TOOL: ToolSpec[CSDIQueryArgs, CSDIQueryResult] = ToolSpec(
    name="csdi.query_features",
    description_en=_build_description(),
    args_schema=CSDIQueryArgs,
    result_schema=CSDIQueryResult,
    handler=_csdi_query_handler,
    ttl_seconds=24 * 60 * 60,  # CSDI catalogs change slowly; cache aggressively.
    budget_ms=3000,
    upstream="portal.csdi.gov.hk",
    upstream_langs=frozenset({"en", "zh-Hant"}),
    safety_class="read",
)
