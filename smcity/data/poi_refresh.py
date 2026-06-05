"""Rebuild the local POI mirror from Overpass — nightly, in-process.

The refresh runs ONE Overpass query per category over the Hong Kong bbox and
swaps the result into `PoiStore` atomically per category. It is driven by:

- `refresh_all(...)` — the work itself (used by the CLI and the loop).
- `nightly_refresh_loop(...)` — an asyncio task spawned from the app lifespan.
  Both blue/green replicas run it, but a `fcntl` advisory lock on the shared
  `/app/state` volume guarantees only ONE replica actually refreshes per cycle;
  the other simply skips. No leader election, no extra service — the lock IS the
  election, and it lives next to the DB it guards.
- `python -m smcity.data.poi_refresh` — a manual/ops entry point (one-shot).

Tag derivation is NOT re-implemented here: `refresh_all` calls the live tool's
`_build_query` (Overpass query) and `_parse_overpass_elements` (element shaping),
so the mirror is byte-for-byte what a live `find_poi` would have returned. That
shared-code guarantee is the whole point — it is the same single-source-of-truth
discipline the category registry established in v0.7.1.
"""

from __future__ import annotations

import argparse
import asyncio
import errno
import fcntl
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

import structlog

from smcity.data.poi_store import PoiStore, get_poi_store
from smcity.settings import get_settings
from smcity.tools.osm_pois import (
    _HK_BBOX,
    _build_query,
    _parse_overpass_elements,
    fetch_overpass,
)
from smcity.tools.poi_categories import CATEGORIES

log = structlog.get_logger("smcity.poi_refresh")


def _lock_path(store_path: str | Path) -> Path:
    """The advisory-lock file sits beside the DB, on the same shared volume."""
    return Path(store_path).with_suffix(".refresh.lock")


async def refresh_category(
    store: PoiStore,
    category: str,
    bbox: tuple[float, float, float, float] = _HK_BBOX,
) -> int:
    """Refresh one category from live Overpass into the store. Returns row count.

    The SQLite write is offloaded to a worker thread: the refresh runs INSIDE the
    request-serving process (both replicas), so a synchronous insert of a large
    category would block the event loop and spike latency for users on the
    refreshing replica — most visibly during the startup warm-up right after a
    deploy. `to_thread` keeps the loop responsive throughout.
    """
    data = await fetch_overpass(_build_query(category, bbox))
    pois = _parse_overpass_elements(data, max_results=None)
    refreshed_at = datetime.now(UTC).isoformat()
    rows = [p.model_dump() for p in pois]
    return await asyncio.to_thread(store.replace_category, category, rows, refreshed_at)


async def refresh_all(
    store: PoiStore | None = None,
    *,
    bbox: tuple[float, float, float, float] = _HK_BBOX,
    throttle_s: float | None = None,
) -> dict[str, int]:
    """Refresh every category, throttled between Overpass calls.

    A failure on one category is logged and skipped — a single 504 must not
    abort the whole sweep or leave the mirror half-rebuilt (each category swap
    is independent and atomic). Returns {category: row_count} for the categories
    that succeeded.
    """
    store = store or get_poi_store()
    settings = get_settings()
    throttle = settings.poi_refresh_throttle_s if throttle_s is None else throttle_s

    results: dict[str, int] = {}
    categories = list(CATEGORIES)
    for i, category in enumerate(categories):
        try:
            count = await refresh_category(store, category, bbox)
            results[category] = count
            log.info("poi_refresh.category", category=category, count=count)
        except Exception as err:  # keep sweeping; one bad category != fatal
            log.warning("poi_refresh.category_failed", category=category, error=str(err))
        if throttle and i < len(categories) - 1:
            await asyncio.sleep(throttle)

    log.info(
        "poi_refresh.done",
        categories=len(results),
        total=sum(results.values()),
    )
    return results


def _acquire_lock(lock_path: Path) -> IO[bytes] | None:
    """Open the lock file and take a non-blocking exclusive flock.

    Returns the open file (keep it alive to hold the lock) or None if another
    process already holds it. Synchronous on purpose: opening a tiny local file
    + flock is a microsecond operation, and the fd must outlive the refresh that
    follows. The caller awaits the actual (slow) Overpass work, not this.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("wb")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as err:
        lock_file.close()
        if err.errno in (errno.EACCES, errno.EAGAIN):
            return None
        raise
    return lock_file


def _release_lock(lock_file: IO[bytes]) -> None:
    with suppress(OSError):
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    lock_file.close()


async def refresh_once_locked(store: PoiStore) -> dict[str, int] | None:
    """Acquire the cross-replica lock and refresh, or return None if a sibling
    replica already holds it (it is doing the refresh; we skip)."""
    lock_file = _acquire_lock(_lock_path(store.path))
    if lock_file is None:
        log.info("poi_refresh.skipped_locked")
        return None
    try:
        return await refresh_all(store)
    finally:
        _release_lock(lock_file)


async def nightly_refresh_loop(
    store: PoiStore | None = None,
    *,
    interval_hours: float | None = None,
) -> None:
    """Forever: refresh the mirror (lock-guarded), then sleep one interval.

    Runs an immediate pass on startup so a cold deploy warms within minutes
    rather than waiting for the first scheduled cycle (until then, `find_poi`
    transparently falls back to live Overpass). Cancellation (app shutdown)
    propagates cleanly via `asyncio.CancelledError`.
    """
    store = store or get_poi_store()
    settings = get_settings()
    hours = settings.poi_refresh_interval_hours if interval_hours is None else interval_hours
    interval_s = hours * 3600.0

    while True:
        try:
            await refresh_once_locked(store)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # loop must survive a bad cycle
            log.warning("poi_refresh.cycle_failed", error=str(err))
        await asyncio.sleep(interval_s)


def main(argv: list[str] | None = None) -> int:
    """CLI: `python -m smcity.data.poi_refresh` — one-shot manual refresh."""
    parser = argparse.ArgumentParser(description="Rebuild the local POI mirror from Overpass.")
    parser.add_argument(
        "--no-lock",
        action="store_true",
        help="Skip the cross-replica lock (use when running outside the app, e.g. on the host).",
    )
    args = parser.parse_args(argv)

    store = get_poi_store()
    if args.no_lock:
        results = asyncio.run(refresh_all(store))
    else:
        locked = asyncio.run(refresh_once_locked(store))
        if locked is None:
            print("Another process holds the refresh lock; nothing to do.")
            return 0
        results = locked

    total = sum(results.values())
    print(f"Refreshed {len(results)} categories, {total} POIs -> {store.path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
