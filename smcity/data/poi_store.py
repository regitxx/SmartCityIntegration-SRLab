"""Local SQLite mirror of OSM POIs, spatially indexed with R*Tree.

Why this exists
---------------
`geo.find_poi` previously hit public `overpass-api.de` on every call. That
endpoint is slow and intermittently 504s — a latency source AND a confounder
for quality measurement (a 504 reads as a `tool_error` that has nothing to do
with the model). This module is a local mirror, refreshed nightly by
`smcity.data.poi_refresh`, queried first by the tool; live Overpass remains a
fallback (behind a settings flag) so cold-start and outages degrade gracefully.

Design notes
------------
- **Model-agnostic.** The store knows nothing about LLMs, pydantic schemas, or
  the tool layer. It stores/returns plain rows whose keys happen to match the
  `OsmPoi` fields, so the caller can do `OsmPoi(**row)`. Keeping the store free
  of `osm_pois` imports also keeps the module graph acyclic
  (`osm_pois` -> `poi_store`, never the reverse).
- **Spatial index.** An R*Tree virtual table over (lat, lng) point boxes prunes
  the bbox query to the relevant region before the category filter — the same
  access pattern Overpass gives us, but in-process and sub-millisecond.
- **Per-operation connections + WAL.** Each call opens a short-lived connection.
  That is thread-safe by construction (the nightly refresh runs in a worker
  thread while request handlers read), and WAL lets the single writer refresh
  without blocking concurrent readers. Only one replica ever writes (the
  `poi_refresh` file-lock guarantees it), so there is never multi-writer
  contention on the shared Docker volume.
- **Freshness is tracked per category in `poi_meta`, not inferred from row
  counts.** A category that genuinely has zero POIs in Hong Kong (rare, but
  possible) still gets a meta row, so `is_populated` is true and the tool does
  NOT fall back to live Overpass forever for an empty-but-fresh category.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from smcity.settings import get_settings

# The fields a stored row exposes — exactly the `OsmPoi` constructor args, so a
# caller can reconstruct the model with `OsmPoi(**row)`. `category` is stored
# for indexing/refresh but is NOT part of this tuple (it is not an OsmPoi field).
_POI_FIELDS: tuple[str, ...] = (
    "osm_type",
    "osm_id",
    "lat",
    "lng",
    "name",
    "name_en",
    "name_zh",
    "address",
    "tags",
)


@dataclass(frozen=True, slots=True)
class MirrorFreshness:
    """Snapshot of how complete and how stale the local mirror is."""

    categories_populated: int  # number of categories with a refresh on record
    total_pois: int  # rows across all categories
    oldest_refresh: str | None  # ISO-8601 UTC of the least-recently refreshed cat
    newest_refresh: str | None  # ISO-8601 UTC of the most-recently refreshed cat


class PoiStore:
    """SQLite + R*Tree mirror of OSM POIs, keyed by category.

    Cheap to construct (it only remembers the path); the schema is created
    lazily on first use so importing this module never touches the disk.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._initialised = False

    @property
    def path(self) -> Path:
        """Filesystem location of the mirror DB (e.g. for the refresh lock)."""
        return self._path

    # --- connection plumbing ------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pois (
                rowid     INTEGER PRIMARY KEY,
                category  TEXT    NOT NULL,
                osm_type  TEXT    NOT NULL,
                osm_id    INTEGER NOT NULL,
                lat       REAL    NOT NULL,
                lng       REAL    NOT NULL,
                name      TEXT,
                name_en   TEXT,
                name_zh   TEXT,
                address   TEXT,
                tags_json TEXT    NOT NULL DEFAULT '{}'
            );
            CREATE UNIQUE INDEX IF NOT EXISTS pois_uniq
                ON pois (category, osm_type, osm_id);
            CREATE INDEX IF NOT EXISTS pois_category ON pois (category);

            CREATE VIRTUAL TABLE IF NOT EXISTS pois_rtree USING rtree(
                rowid, min_lat, max_lat, min_lng, max_lng
            );

            CREATE TABLE IF NOT EXISTS poi_meta (
                category     TEXT PRIMARY KEY,
                refreshed_at TEXT NOT NULL,   -- ISO-8601 UTC
                count        INTEGER NOT NULL
            );
            """
        )

    def init(self) -> None:
        """Create the schema if needed (idempotent)."""
        if self._initialised:
            return
        with closing(self._connect()) as conn, conn:
            self._ensure_schema(conn)
        self._initialised = True

    # --- writes -------------------------------------------------------------

    def replace_category(
        self,
        category: str,
        pois: Iterable[Mapping[str, Any]],
        refreshed_at: str,
    ) -> int:
        """Atomically swap all rows for one category and stamp its freshness.

        `pois` is an iterable of mappings carrying the `_POI_FIELDS` keys (e.g.
        `OsmPoi.model_dump()`). The delete+insert+meta-update runs in one
        transaction so a reader never sees a half-rebuilt category. Returns the
        number of rows written.
        """
        self.init()
        rows = list(pois)
        with closing(self._connect()) as conn, conn:
            # Drop the category's existing rows from both tables. The R*Tree has
            # no category column, so collect the rowids first.
            old_ids = [
                r["rowid"]
                for r in conn.execute("SELECT rowid FROM pois WHERE category = ?", (category,))
            ]
            conn.execute("DELETE FROM pois WHERE category = ?", (category,))
            conn.executemany("DELETE FROM pois_rtree WHERE rowid = ?", [(i,) for i in old_ids])

            for poi in rows:
                lat = float(poi["lat"])
                lng = float(poi["lng"])
                cur = conn.execute(
                    """
                    INSERT INTO pois
                        (category, osm_type, osm_id, lat, lng,
                         name, name_en, name_zh, address, tags_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        category,
                        poi["osm_type"],
                        int(poi["osm_id"]),
                        lat,
                        lng,
                        poi.get("name"),
                        poi.get("name_en"),
                        poi.get("name_zh"),
                        poi.get("address"),
                        json.dumps(poi.get("tags") or {}, ensure_ascii=False),
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO pois_rtree (rowid, min_lat, max_lat, min_lng, max_lng)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (cur.lastrowid, lat, lat, lng, lng),
                )

            conn.execute(
                """
                INSERT INTO poi_meta (category, refreshed_at, count)
                VALUES (?, ?, ?)
                ON CONFLICT(category) DO UPDATE SET
                    refreshed_at = excluded.refreshed_at,
                    count        = excluded.count
                """,
                (category, refreshed_at, len(rows)),
            )
        return len(rows)

    # --- reads --------------------------------------------------------------

    def is_populated(self, category: str) -> bool:
        """True if the category has ever been refreshed (even to zero rows)."""
        self.init()
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT 1 FROM poi_meta WHERE category = ? LIMIT 1", (category,)
            ).fetchone()
        return row is not None

    def query(
        self,
        category: str,
        bbox: tuple[float, float, float, float],
        limit: int,
    ) -> list[dict[str, Any]]:
        """Return POIs of `category` inside `bbox` (min_lat, min_lng, max_lat, max_lng).

        Rows are returned in R*Tree order, capped at `limit`. Each dict carries
        exactly the `_POI_FIELDS` keys, ready for `OsmPoi(**row)`.
        """
        self.init()
        min_lat, min_lng, max_lat, max_lng = bbox
        with closing(self._connect()) as conn:
            cur = conn.execute(
                """
                SELECT p.osm_type, p.osm_id, p.lat, p.lng,
                       p.name, p.name_en, p.name_zh, p.address, p.tags_json
                FROM pois_rtree r
                JOIN pois p ON p.rowid = r.rowid
                WHERE r.min_lat >= ? AND r.max_lat <= ?
                  AND r.min_lng >= ? AND r.max_lng <= ?
                  AND p.category = ?
                LIMIT ?
                """,
                (min_lat, max_lat, min_lng, max_lng, category, limit),
            )
            out: list[dict[str, Any]] = []
            for r in cur:
                out.append(
                    {
                        "osm_type": r["osm_type"],
                        "osm_id": r["osm_id"],
                        "lat": r["lat"],
                        "lng": r["lng"],
                        "name": r["name"],
                        "name_en": r["name_en"],
                        "name_zh": r["name_zh"],
                        "address": r["address"],
                        "tags": json.loads(r["tags_json"]),
                    }
                )
        return out

    def freshness(self) -> MirrorFreshness:
        """Aggregate freshness across all refreshed categories."""
        self.init()
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*)              AS cats,
                       COALESCE(SUM(count),0) AS total,
                       MIN(refreshed_at)      AS oldest,
                       MAX(refreshed_at)      AS newest
                FROM poi_meta
                """
            ).fetchone()
        return MirrorFreshness(
            categories_populated=row["cats"],
            total_pois=row["total"],
            oldest_refresh=row["oldest"],
            newest_refresh=row["newest"],
        )


@cache
def get_poi_store() -> PoiStore:
    """Process-wide singleton, path-driven by settings.

    Memoised so the request handlers and the nightly refresh task share one
    `PoiStore` (and therefore one DB file). Tests that need a fresh path call
    `get_poi_store.cache_clear()` after overriding `POI_STORE_PATH`.
    """
    return PoiStore(get_settings().poi_store_path)
