"""
Periodic ``candles_*`` retention job (#274 AC3 — mandatory, blocking).

Caps every ``candles_{pair}_{timeframe}`` Mongo collection at a maximum
document count instead of a calendar-day cutoff. Per
``docs/candle-consumer-retention-contract.md`` AC4, a capped-count job is the
recommended approach for this namespace: a fixed-count cap naturally stays
bounded regardless of gaps in candle cadence, whereas ``klines_retention.py``'s
calendar-day approach assumes a roughly steady insertion rate that does not
hold for the newer, lower-volume ``candles_*`` write path.

Before this module, ``candles_*`` collections were **never** discovered or
pruned by ``klines_retention.py`` (its ``KLINES_COLLECTION_PREFIX`` filter only
matches ``klines_``) — the exact gap flagged as blocking by #274 AC3 and
confirmed unaddressed by the 2026-09-11 quality audit. This closes it.

    python -m data_manager.maintenance.candles_retention [--dry-run]

The default cap (``CANDLES_RETENTION_MAX_COUNT``, default
``constants.CANDLE_WARMUP_MIN_CANDLES`` = 400) mirrors the warm-up/readiness
depth from data-manager#275/#276 so a freshly cut-over collection is never
trimmed below the depth it was just backfilled to.

See ``docs/candles-retention.md`` and the umbrella incident
https://github.com/PetroSa2/petrosa_k8s/issues/783 for context.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

import constants
from data_manager.db.mongodb_adapter import MongoDBAdapter

logger = logging.getLogger(__name__)

CANDLES_COLLECTION_PREFIX = "candles_"

# Approximate per-document footprint from
# docs/candle-consumer-retention-contract.md AC3 (~230 bytes raw BSON + ~100
# bytes/doc index overhead). Used only to surface an estimated bytes-reclaimed
# figure in the completion log (#274 AC5); not an exact accounting.
BYTES_PER_CANDLE_ESTIMATE = 330

# Retention floor: mirrors the #275/#276 warm-up depth so a freshly cut-over
# collection never sheds candles it just backfilled. Overridable via
# CANDLES_RETENTION_MAX_COUNT for operators who want headroom above the
# warm-up floor.
DEFAULT_MAX_CANDLES = constants.CANDLE_WARMUP_MIN_CANDLES

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass
class CandlesRetentionResult:
    """Per-collection outcome of a candles retention run."""

    collection: str
    max_count: int
    count_before: int
    docs_deleted: int
    dry_run: bool


def parse_candles_collection(collection_name: str) -> tuple[str, str] | None:
    """Split a ``candles_{symbol}_{timeframe}`` name into ``(symbol, timeframe)``.

    Returns ``None`` for anything that doesn't match the shape (missing
    prefix, no separator, or an empty symbol/timeframe part).
    """
    if not collection_name.startswith(CANDLES_COLLECTION_PREFIX):
        return None
    remainder = collection_name[len(CANDLES_COLLECTION_PREFIX) :]
    symbol, sep, timeframe = remainder.rpartition("_")
    if not sep or not symbol or not timeframe:
        return None
    return symbol, timeframe


async def discover_candles_collections(adapter: MongoDBAdapter) -> list[str]:
    """Return sorted list of ``candles_*`` collections present in the DB."""
    collections = await adapter.list_collections()
    return sorted(c for c in collections if c.startswith(CANDLES_COLLECTION_PREFIX))


async def prune_candles_collection(
    adapter: MongoDBAdapter,
    collection: str,
    *,
    max_count: int,
    dry_run: bool,
) -> CandlesRetentionResult:
    """Delete every doc in ``collection`` except the newest ``max_count``.

    Never raises: caller-facing failures are logged and surfaced as a
    zero-deletion result so one bad collection cannot abort the whole run.
    """
    try:
        count_before = await adapter.get_record_count(collection)
    except Exception as e:
        logger.error("candles_retention: %s — count failed: %s", collection, e)
        return CandlesRetentionResult(
            collection=collection,
            max_count=max_count,
            count_before=0,
            docs_deleted=0,
            dry_run=dry_run,
        )

    if count_before <= max_count:
        logger.info(
            "candles_retention: %s — %d docs <= cap %d, nothing to trim",
            collection,
            count_before,
            max_count,
        )
        return CandlesRetentionResult(
            collection=collection,
            max_count=max_count,
            count_before=count_before,
            docs_deleted=0,
            dry_run=dry_run,
        )

    try:
        # Anchor the cutoff at the boundary between the newest `max_count`
        # docs (kept) and everything older (pruned): the timestamp of the
        # Nth-newest candle.
        newest_window = await adapter.query_latest(collection, limit=max_count)
    except Exception as e:
        logger.error(
            "candles_retention: %s — query_latest failed, skipping: %s", collection, e
        )
        return CandlesRetentionResult(
            collection=collection,
            max_count=max_count,
            count_before=count_before,
            docs_deleted=0,
            dry_run=dry_run,
        )

    if not newest_window:
        logger.warning(
            "candles_retention: %s — count=%d but query_latest returned nothing; skipping",
            collection,
            count_before,
        )
        return CandlesRetentionResult(
            collection=collection,
            max_count=max_count,
            count_before=count_before,
            docs_deleted=0,
            dry_run=dry_run,
        )

    boundary = newest_window[-1].get("timestamp")
    if not isinstance(boundary, datetime):
        logger.warning(
            "candles_retention: %s — Nth-newest candle has no usable timestamp "
            "(%r); skipping",
            collection,
            boundary,
        )
        return CandlesRetentionResult(
            collection=collection,
            max_count=max_count,
            count_before=count_before,
            docs_deleted=0,
            dry_run=dry_run,
        )
    if boundary.tzinfo is None:
        boundary = boundary.replace(tzinfo=UTC)

    try:
        if dry_run:
            docs_deleted = await adapter.get_record_count(collection, end=boundary)
        else:
            docs_deleted = await adapter.delete_range(
                collection, start=_EPOCH, end=boundary
            )
    except Exception as e:
        logger.error(
            "candles_retention: %s — delete/count at cutoff failed: %s", collection, e
        )
        return CandlesRetentionResult(
            collection=collection,
            max_count=max_count,
            count_before=count_before,
            docs_deleted=0,
            dry_run=dry_run,
        )

    est_bytes = docs_deleted * BYTES_PER_CANDLE_ESTIMATE
    logger.info(
        "candles_retention: %s — %d docs before, cap %d, %d docs %s "
        "(~%d bytes est.), cutoff=%s",
        collection,
        count_before,
        max_count,
        docs_deleted,
        "would-be-deleted" if dry_run else "deleted",
        est_bytes,
        boundary.isoformat(),
    )
    return CandlesRetentionResult(
        collection=collection,
        max_count=max_count,
        count_before=count_before,
        docs_deleted=docs_deleted,
        dry_run=dry_run,
    )


async def prune_candles(
    adapter: MongoDBAdapter,
    *,
    max_count: int | None = None,
    dry_run: bool = False,
    collections_override: list[str] | None = None,
) -> list[CandlesRetentionResult]:
    """Run retention across every discoverable ``candles_*`` collection."""
    effective_max = max_count if max_count is not None else _resolve_max_count()

    if collections_override is not None:
        collections = list(collections_override)
    else:
        collections = await discover_candles_collections(adapter)

    if not collections:
        logger.info("candles_retention: no candles_* collections found, nothing to do")
        return []

    results: list[CandlesRetentionResult] = []
    for collection in collections:
        results.append(
            await prune_candles_collection(
                adapter, collection, max_count=effective_max, dry_run=dry_run
            )
        )

    total_deleted = sum(r.docs_deleted for r in results)
    total_before = sum(r.count_before for r in results)
    total_est_bytes = total_deleted * BYTES_PER_CANDLE_ESTIMATE
    logger.info(
        "candles_retention: run complete — %d collections processed, %d/%d docs %s "
        "(~%d bytes est. reclaimed)",
        len(results),
        total_deleted,
        total_before,
        "would-be-deleted" if dry_run else "deleted",
        total_est_bytes,
    )
    return results


def _resolve_max_count() -> int:
    raw = os.getenv("CANDLES_RETENTION_MAX_COUNT")
    if raw is None:
        return DEFAULT_MAX_CANDLES
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Ignoring non-integer CANDLES_RETENTION_MAX_COUNT=%r; using default %d",
            raw,
            DEFAULT_MAX_CANDLES,
        )
        return DEFAULT_MAX_CANDLES
    if value < 1:
        logger.warning("Clamping CANDLES_RETENTION_MAX_COUNT=%d to minimum 1", value)
        return 1
    return value


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m data_manager.maintenance.candles_retention",
        description=(
            "Cap every candles_* Mongo collection at a maximum document count "
            "(data-manager#274 AC3)."
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
        "--max-count",
        type=int,
        default=None,
        help="Override CANDLES_RETENTION_MAX_COUNT for this run.",
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

    collections_override = None
    if args.collections:
        collections_override = [
            c.strip() for c in args.collections.split(",") if c.strip()
        ]

    connection_string = os.getenv("MONGODB_URL")
    if not connection_string:
        logger.error("MONGODB_URL is not set; cannot connect to MongoDB")
        return 2

    adapter = MongoDBAdapter(connection_string=connection_string)
    adapter.connect()
    try:
        await prune_candles(
            adapter,
            max_count=args.max_count,
            dry_run=args.dry_run,
            collections_override=collections_override,
        )
    finally:
        adapter.disconnect()
    return 0


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    sys.exit(main())
