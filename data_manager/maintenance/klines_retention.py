"""
Periodic klines-retention job (AC2 of petrosa_k8s#783).

Deletes klines older than a configurable per-timeframe retention window from
MongoDB and, when enabled, MySQL. Designed to be invoked from a Kubernetes
CronJob via:

    python -m data_manager.maintenance.klines_retention [--dry-run]

The job uses the existing adapter primitives and walks the deletion window in
day-sized chunks so a long backlog is reclaimed in bounded steps rather than a
single multi-million-row delete.

Retention windows default to comfortably more than the longest strategy
lookback per timeframe and are overridable per timeframe via env vars of the
form ``KLINES_RETENTION_DAYS_<TIMEFRAME>`` (e.g. ``KLINES_RETENTION_DAYS_1H``).

See ``docs/klines-retention.md`` and the umbrella incident at
https://github.com/PetroSa2/petrosa_k8s/issues/783 for context.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast, runtime_checkable

import constants
from data_manager.db import mysql_adapter
from data_manager.db.mongodb_adapter import MongoDBAdapter
from data_manager.db.mysql_adapter import MySQLAdapter

logger = logging.getLogger(__name__)

KLINES_COLLECTION_PREFIX = "klines_"

# Per-timeframe retention windows in days. Each default exceeds the longest
# strategy lookback at that timeframe by a comfortable margin so analysis is
# never starved by deletion. Operators override per-timeframe via env:
# ``KLINES_RETENTION_DAYS_<TIMEFRAME>``.
DEFAULT_RETENTION_DAYS: dict[str, int] = {
    "1m": 7,
    "3m": 14,
    "5m": 14,
    "15m": 30,
    "30m": 30,
    "1h": 90,
    "2h": 90,
    "4h": 180,
    "6h": 180,
    "8h": 180,
    "12h": 365,
    "1d": 450,
    "3d": 365,
    "1w": 730,
    # `1M` (monthly, uppercase) intentionally omitted — its env-var name
    # would collide with `1m` after upper-casing. If/when monthly klines
    # are ever persisted, file a follow-up and pick a disambiguated env
    # key (e.g. `KLINES_RETENTION_DAYS_MONTHLY`). For now, monthly falls
    # through to FALLBACK_RETENTION_DAYS.
}

DEFAULT_BATCH_DAYS = 1
DEFAULT_MAX_CHUNKS_PER_COLLECTION = 400
# Fallback window when a collection's timeframe is unknown (e.g. a future
# timeframe lands before this map is updated). Conservative: keep ~1 year.
FALLBACK_RETENTION_DAYS = 365


@dataclass
class RetentionConfig:
    """Resolved configuration for one retention run."""

    windows_days: dict[str, int] = field(default_factory=dict)
    batch_days: int = DEFAULT_BATCH_DAYS
    max_chunks_per_collection: int = DEFAULT_MAX_CHUNKS_PER_COLLECTION
    dry_run: bool = False
    collections_override: list[str] | None = None
    backends: tuple[str, ...] = ("mongodb",)
    mysql_dry_run: bool = True
    mysql_chunk_sleep_ms: int = 250
    mysql_max_rows_per_chunk: int = 50_000


@dataclass
class RetentionResult:
    """Per-collection outcome of a retention run."""

    collection: str
    timeframe: str | None
    cutoff: datetime
    chunks_processed: int
    docs_deleted: int
    capped: bool
    dry_run: bool
    backend: str = "mongodb"


@runtime_checkable
class RetentionBackend(Protocol):
    """Small async retention surface shared by MongoDB and MySQL."""

    name: str

    async def list_klines_collections(self) -> list[str]: ...

    async def count_range(
        self,
        collection: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> int: ...

    async def oldest_timestamp(
        self, collection: str, *, before: datetime
    ) -> datetime | None: ...

    async def delete_range(
        self, collection: str, *, start: datetime, end: datetime
    ) -> int: ...


class MongoRetentionBackend:
    """Retention backend preserving the existing MongoDB adapter behaviour."""

    name = "mongodb"

    def __init__(self, adapter: MongoDBAdapter):
        self.adapter = adapter

    async def list_klines_collections(self) -> list[str]:
        collections = await self.adapter.list_collections()
        return sorted(c for c in collections if c.startswith(KLINES_COLLECTION_PREFIX))

    async def count_range(
        self,
        collection: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> int:
        bounds = {}
        if start is not None:
            bounds["start"] = start
        if end is not None:
            bounds["end"] = end
        return await self.adapter.get_record_count(collection, **bounds)

    async def oldest_timestamp(
        self, collection: str, *, before: datetime
    ) -> datetime | None:
        oldest = await self.adapter.query_range(
            collection,
            start=datetime(1970, 1, 1, tzinfo=UTC),
            end=before,
        )
        return oldest[0]["timestamp"] if oldest else None

    async def delete_range(
        self, collection: str, *, start: datetime, end: datetime
    ) -> int:
        return await self.adapter.delete_range(collection, start=start, end=end)


class MySQLRetentionBackend:
    """Async facade over the synchronous MySQL adapter primitives."""

    name = "mysql"

    def __init__(self, adapter: MySQLAdapter, *, schema: str | None = None):
        self.adapter = adapter
        self.schema = schema or constants.MYSQL_DB

    async def list_klines_collections(self) -> list[str]:
        engine = getattr(self.adapter, "engine", None)
        if engine is None:
            raise RuntimeError("MySQL adapter has no connected engine")
        inventory = await asyncio.to_thread(
            mysql_adapter.table_inventory, engine, self.schema
        )
        return sorted(
            str(row.get("TABLE_NAME", row.get("table_name", "")))
            for row in inventory
            if str(row.get("TABLE_TYPE", row.get("table_type", ""))).upper()
            == "BASE TABLE"
            and str(row.get("TABLE_NAME", row.get("table_name", ""))).startswith(
                KLINES_COLLECTION_PREFIX
            )
        )

    async def count_range(
        self,
        collection: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> int:
        return await asyncio.to_thread(
            self.adapter.get_record_count,
            collection,
            start=start,
            end=end,
        )

    async def oldest_timestamp(
        self, collection: str, *, before: datetime
    ) -> datetime | None:
        oldest = await asyncio.to_thread(
            self.adapter.query_range,
            collection,
            datetime(1970, 1, 1, tzinfo=UTC),
            before,
            limit=1,
        )
        return oldest[0]["timestamp"] if oldest else None

    async def delete_range(
        self, collection: str, *, start: datetime, end: datetime
    ) -> int:
        return await asyncio.to_thread(
            self.adapter.delete_range, collection, start=start, end=end
        )


def _coerce_backend(value: object) -> RetentionBackend:
    """Accept the legacy Mongo adapter shape while exposing the new protocol."""
    if isinstance(value, MongoRetentionBackend | MySQLRetentionBackend):
        return value
    required = (
        "list_klines_collections",
        "count_range",
        "oldest_timestamp",
        "delete_range",
    )
    if all(callable(getattr(type(value), method, None)) for method in required):
        return cast(RetentionBackend, value)
    return MongoRetentionBackend(cast(MongoDBAdapter, value))


def parse_timeframe(collection_name: str) -> str | None:
    """Return the timeframe suffix of a ``klines_<tf>`` collection, or ``None``."""
    if not collection_name.startswith(KLINES_COLLECTION_PREFIX):
        return None
    suffix = collection_name[len(KLINES_COLLECTION_PREFIX) :]
    return suffix or None


def normalize_timeframe(timeframe: str | None) -> str | None:
    """Convert MySQL's financial suffixes to Binance timeframe keys."""
    if not timeframe:
        return timeframe
    if timeframe[0] in {"m", "h", "d"} and timeframe[1:].isdigit():
        return f"{timeframe[1:]}{timeframe[0]}"
    return timeframe


def resolve_window_days(timeframe: str | None, windows: dict[str, int]) -> int:
    """Resolve the retention-window days for a timeframe with sane fallback."""
    if timeframe and timeframe in windows:
        return windows[timeframe]
    if timeframe and timeframe in DEFAULT_RETENTION_DAYS:
        return DEFAULT_RETENTION_DAYS[timeframe]
    return FALLBACK_RETENTION_DAYS


def compute_cutoff(now: datetime, days: int) -> datetime:
    """Return the inclusive upper bound on docs eligible for deletion."""
    return now - timedelta(days=days)


def load_config_from_env(environ: dict[str, str] | None = None) -> RetentionConfig:
    """Build a ``RetentionConfig`` from environment variables."""
    env = environ if environ is not None else os.environ

    windows = dict(DEFAULT_RETENTION_DAYS)
    for tf in list(DEFAULT_RETENTION_DAYS):
        env_key = f"KLINES_RETENTION_DAYS_{tf.upper()}"
        raw = env.get(env_key)
        if raw is None:
            continue
        try:
            windows[tf] = int(raw)
        except ValueError:
            logger.warning(
                "Ignoring non-integer %s=%r; falling back to default %d days",
                env_key,
                raw,
                windows[tf],
            )

    batch_days = _parse_int_env(
        env, "KLINES_RETENTION_BATCH_DAYS", DEFAULT_BATCH_DAYS, minimum=1
    )
    max_chunks = _parse_int_env(
        env,
        "KLINES_RETENTION_MAX_CHUNKS_PER_COLLECTION",
        DEFAULT_MAX_CHUNKS_PER_COLLECTION,
        minimum=1,
    )
    dry_run = env.get("KLINES_RETENTION_DRY_RUN", "").lower() == "true"
    backends = tuple(
        backend
        for backend in (
            value.strip().lower()
            for value in env.get("KLINES_RETENTION_BACKENDS", "mongodb").split(",")
        )
        if backend in {"mongodb", "mysql"}
    ) or ("mongodb",)
    mysql_chunk_sleep_ms = _parse_int_env(
        env, "KLINES_RETENTION_MYSQL_CHUNK_SLEEP_MS", 250, minimum=0
    )
    mysql_max_rows_per_chunk = _parse_int_env(
        env, "KLINES_RETENTION_MYSQL_MAX_ROWS_PER_CHUNK", 50_000, minimum=1
    )

    return RetentionConfig(
        windows_days=windows,
        batch_days=batch_days,
        max_chunks_per_collection=max_chunks,
        dry_run=dry_run,
        backends=backends,
        mysql_dry_run=env.get("KLINES_RETENTION_MYSQL_DRY_RUN", "true").lower()
        == "true",
        mysql_chunk_sleep_ms=mysql_chunk_sleep_ms,
        mysql_max_rows_per_chunk=mysql_max_rows_per_chunk,
    )


def _parse_int_env(
    env: Mapping[str, str], key: str, default: int, *, minimum: int
) -> int:
    raw = env.get(key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Ignoring non-integer %s=%r; using default %d", key, raw, default
        )
        return default
    if value < minimum:
        logger.warning(
            "Clamping %s=%d to minimum %d to keep retention bounded",
            key,
            value,
            minimum,
        )
        return minimum
    return value


async def discover_klines_collections(adapter: MongoDBAdapter) -> list[str]:
    """Return sorted list of ``klines_*`` collections present in the DB."""
    return await MongoRetentionBackend(adapter).list_klines_collections()


async def prune_collection(
    backend: RetentionBackend | MongoDBAdapter,
    collection: str,
    cutoff: datetime,
    *,
    batch_days: int,
    max_chunks: int,
    dry_run: bool,
    mysql_chunk_sleep_ms: int = 250,
    mysql_max_rows_per_chunk: int = 50_000,
) -> RetentionResult:
    """Delete docs older than ``cutoff`` from ``collection`` in bounded chunks.

    Walks the deletion window from the oldest doc forward in ``batch_days``-day
    chunks. Each chunk is a single ``delete_range`` call (the existing adapter
    primitive). The walk stops at ``max_chunks`` to keep one run's blast radius
    bounded — the next scheduled run picks up where this one left off.
    """
    backend = _coerce_backend(backend)

    timeframe = parse_timeframe(collection)
    total_eligible = await backend.count_range(collection, end=cutoff)

    if total_eligible == 0:
        logger.info(
            "klines_retention: %s — nothing to delete (cutoff=%s)",
            collection,
            cutoff.isoformat(),
        )
        return RetentionResult(
            collection=collection,
            timeframe=timeframe,
            cutoff=cutoff,
            chunks_processed=0,
            docs_deleted=0,
            capped=False,
            dry_run=dry_run,
            backend=backend.name,
        )

    # Anchor the walk at the oldest doc actually present so we don't iterate
    # through years of empty time when a collection's history starts recently.
    oldest_timestamp = await backend.oldest_timestamp(collection, before=cutoff)
    if oldest_timestamp is None:
        return RetentionResult(
            collection=collection,
            timeframe=timeframe,
            cutoff=cutoff,
            chunks_processed=0,
            docs_deleted=0,
            capped=False,
            dry_run=dry_run,
            backend=backend.name,
        )
    walk_start = _ensure_aware(oldest_timestamp)

    chunk_size = timedelta(days=batch_days)
    chunks_processed = 0
    docs_deleted = 0
    capped = False
    current = walk_start

    while current < cutoff and chunks_processed < max_chunks:
        chunk_end = min(current + chunk_size, cutoff)
        if backend.name == "mysql":
            chunk_count = await backend.count_range(
                collection, start=current, end=chunk_end
            )
            while (
                chunk_count > mysql_max_rows_per_chunk
                and chunk_end - current > timedelta(hours=1)
            ):
                chunk_end = current + (chunk_end - current) / 2
                chunk_count = await backend.count_range(
                    collection, start=current, end=chunk_end
                )
            if dry_run:
                docs_in_chunk = chunk_count
            else:
                docs_in_chunk = await backend.delete_range(
                    collection, start=current, end=chunk_end
                )
        elif dry_run:
            docs_in_chunk = await backend.count_range(
                collection, start=current, end=chunk_end
            )
        else:
            docs_in_chunk = await backend.delete_range(
                collection, start=current, end=chunk_end
            )
        chunks_processed += 1
        docs_deleted += docs_in_chunk
        logger.info(
            "klines_retention: %s chunk %d %s → %s — %d docs%s",
            collection,
            chunks_processed,
            current.isoformat(),
            chunk_end.isoformat(),
            docs_in_chunk,
            " (dry-run)" if dry_run else "",
        )
        current = chunk_end
        if (
            backend.name == "mysql"
            and current < cutoff
            and chunks_processed < max_chunks
            and mysql_chunk_sleep_ms
        ):
            await asyncio.sleep(mysql_chunk_sleep_ms / 1000)

    if current < cutoff:
        capped = True
        logger.warning(
            "klines_retention: %s reached max_chunks=%d before cutoff (last=%s, cutoff=%s)",
            collection,
            max_chunks,
            current.isoformat(),
            cutoff.isoformat(),
        )

    return RetentionResult(
        collection=collection,
        timeframe=timeframe,
        cutoff=cutoff,
        chunks_processed=chunks_processed,
        docs_deleted=docs_deleted,
        capped=capped,
        dry_run=dry_run,
        backend=backend.name,
    )


async def prune_klines(
    backends: RetentionBackend | Sequence[RetentionBackend] | MongoDBAdapter,
    config: RetentionConfig,
    *,
    now: datetime | None = None,
) -> list[RetentionResult]:
    """Run retention across every discoverable ``klines_*`` collection."""
    effective_now = now if now is not None else datetime.now(UTC)

    results: list[RetentionResult] = []
    if isinstance(backends, Sequence) and not isinstance(backends, str | bytes):
        backend_list = [_coerce_backend(item) for item in backends]
    else:
        backend_list = [_coerce_backend(backends)]

    for backend in backend_list:
        if config.collections_override is not None:
            collections = list(config.collections_override)
        else:
            collections = await backend.list_klines_collections()

        if not collections:
            logger.info(
                "klines_retention: no klines_* collections found for backend=%s",
                backend.name,
            )
            continue

        backend_dry_run = (
            config.dry_run if backend.name == "mongodb" else config.mysql_dry_run
        )
        for collection in collections:
            timeframe = parse_timeframe(collection)
            if backend.name == "mysql":
                timeframe = normalize_timeframe(timeframe)
            window_days = resolve_window_days(timeframe, config.windows_days)
            cutoff = compute_cutoff(effective_now, window_days)
            logger.info(
                "klines_retention: backend=%s %s timeframe=%s window_days=%d cutoff=%s%s",
                backend.name,
                collection,
                timeframe or "?",
                window_days,
                cutoff.isoformat(),
                " (dry-run)" if backend_dry_run else "",
            )
            result = await prune_collection(
                backend,
                collection,
                cutoff,
                batch_days=config.batch_days,
                max_chunks=config.max_chunks_per_collection,
                dry_run=backend_dry_run,
                mysql_chunk_sleep_ms=config.mysql_chunk_sleep_ms,
                mysql_max_rows_per_chunk=config.mysql_max_rows_per_chunk,
            )
            results.append(result)

    totals_by_backend: dict[str, int] = {}
    for result in results:
        totals_by_backend[result.backend] = (
            totals_by_backend.get(result.backend, 0) + result.docs_deleted
        )
    capped_collections = [r.collection for r in results if r.capped]
    logger.info(
        "klines_retention: run complete — %d collections processed, docs per backend=%s %s%s",
        len(results),
        totals_by_backend,
        "would-be-deleted" if config.dry_run else "deleted",
        f", capped collections: {capped_collections}" if capped_collections else "",
    )
    return results


def _ensure_aware(value: datetime) -> datetime:
    """Return ``value`` with a UTC tzinfo when it was stored naive."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m data_manager.maintenance.klines_retention",
        description=(
            "Delete klines older than the configured per-timeframe retention "
            "window from every klines_* backend collection."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count what would be deleted without modifying any data.",
    )
    parser.add_argument(
        "--collections",
        help=(
            "Comma-separated list of collections to operate on. Overrides discovery."
        ),
    )
    parser.add_argument(
        "--batch-days",
        type=int,
        default=None,
        help="Override KLINES_RETENTION_BATCH_DAYS for this run.",
    )
    parser.add_argument(
        "--max-chunks",
        type=int,
        default=None,
        help="Override KLINES_RETENTION_MAX_CHUNKS_PER_COLLECTION for this run.",
    )
    return parser


def _configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


async def _amain(argv: list[str] | None = None) -> int:
    parser = _build_argparser()
    args = parser.parse_args(argv)

    config = load_config_from_env()
    if args.dry_run:
        config.dry_run = True
    if args.collections:
        config.collections_override = [
            c.strip() for c in args.collections.split(",") if c.strip()
        ]
    if args.batch_days is not None:
        config.batch_days = max(1, args.batch_days)
    if args.max_chunks is not None:
        config.max_chunks_per_collection = max(1, args.max_chunks)

    backends: list[RetentionBackend] = []
    mongo_adapter: MongoDBAdapter | None = None
    mysql_adapter_instance: MySQLAdapter | None = None
    if "mongodb" in config.backends:
        connection_string = os.getenv("MONGODB_URL")
        if not connection_string:
            logger.error("MONGODB_URL is not set; cannot connect to MongoDB")
            return 2
        mongo_adapter = MongoDBAdapter(connection_string=connection_string)
        mongo_adapter.connect()
        backends.append(MongoRetentionBackend(mongo_adapter))

    if "mysql" in config.backends:
        mysql_uri = getattr(constants, "MYSQL_URI", None)
        if not mysql_uri:
            logger.warning("MYSQL_URI is not set; skipping MySQL retention backend")
        else:
            try:
                mysql_adapter_instance = MySQLAdapter(connection_string=mysql_uri)
                mysql_adapter_instance.connect()
                backends.append(MySQLRetentionBackend(mysql_adapter_instance))
            except Exception as exc:
                logger.warning(
                    "MySQL retention backend unavailable; continuing with MongoDB: %s",
                    exc,
                )
                if mysql_adapter_instance is not None:
                    mysql_adapter_instance.disconnect()
    try:
        await prune_klines(backends, config)
    finally:
        if mysql_adapter_instance is not None:
            mysql_adapter_instance.disconnect()
        if mongo_adapter is not None:
            mongo_adapter.disconnect()
    return 0


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    sys.exit(main())
