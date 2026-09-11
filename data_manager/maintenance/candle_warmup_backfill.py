"""
Mongo candle warm-up backfill (AC1 of petrosa-data-manager#275).

Copies the most recent ``CANDLE_WARMUP_MIN_CANDLES`` candles for every
``SUPPORTED_PAIRS x SUPPORTED_INTERVALS`` combination out of the MySQL
``klines_*`` tables (today's write path) into the Mongo
``candles_{pair}_{timeframe}`` collections (the execution read path after
#274's flip), so that promoting MongoDB to primary cannot starve strategy
evaluation.

Usage::

    python -m data_manager.maintenance.candle_warmup_backfill [--dry-run]

Properties:

- **Idempotent.** ``MongoDBAdapter.write`` derives ``_id`` from
  ``symbol``+``timestamp`` and inserts with ``ordered=False``, so re-running
  the job re-writes nothing and never duplicates a candle.
- **Resumable.** Each ``(pair, timeframe)`` is independent, and a collection
  that already passes the #275 AC2 readiness gate is skipped unless
  ``--force`` is given. A run interrupted halfway simply continues on the
  next invocation.
- **Self-bounding.** Per the standing operator rule (never write to MongoDB
  without a bound and an off-flag — four Atlas M0 quota P0s:
  k8s#783/#819/#881/#899), the job trims every collection it touches back to
  ``CANDLE_WARMUP_MIN_CANDLES * CANDLE_WARMUP_TRIM_FACTOR`` documents. Disable
  with ``CANDLE_WARMUP_TRIM_ENABLED=false``.

See ``docs/candle-cutover-runbook.md`` and the window contract in
``docs/candle-consumer-retention-contract.md`` (#276).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import constants
from data_manager.db.mongodb_adapter import MongoDBAdapter
from data_manager.db.mysql_adapter import MySQLAdapter
from data_manager.db.repositories.candle_repository import mysql_table_name
from data_manager.maintenance.candle_readiness import (
    collection_name,
    evaluate_collection,
)
from data_manager.models.market_data import Candle

logger = logging.getLogger(__name__)

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

__all__ = [
    "BackfillConfig",
    "BackfillResult",
    "backfill_pair",
    "load_config_from_env",
    "main",
    "mysql_table_name",
    "row_to_candle",
    "run_backfill",
]


@dataclass
class BackfillConfig:
    """Resolved configuration for one warm-up run."""

    pairs: list[str] = field(default_factory=list)
    timeframes: list[str] = field(default_factory=list)
    min_candles: int = 400
    batch_size: int = 500
    freshness_intervals: float = 3.0
    trim_enabled: bool = True
    trim_factor: float = 1.5
    force: bool = False
    dry_run: bool = False


@dataclass
class BackfillResult:
    """Per-``(pair, timeframe)`` outcome of a warm-up run."""

    pair: str
    timeframe: str
    collection: str
    source_table: str
    skipped: bool = False
    skip_reason: str | None = None
    source_rows: int = 0
    written: int = 0
    trimmed: int = 0
    error: str | None = None
    dry_run: bool = False


def load_config_from_env(environ: dict[str, str] | None = None) -> BackfillConfig:
    """Build a ``BackfillConfig`` from constants (which already read env)."""
    env = environ if environ is not None else dict(os.environ)

    def _int(key: str, default: int, *, minimum: int = 1) -> int:
        raw = env.get(key)
        if raw is None:
            return default
        try:
            value = int(raw)
        except ValueError:
            logger.warning("Ignoring non-integer %s=%r; using %d", key, raw, default)
            return default
        return max(minimum, value)

    return BackfillConfig(
        pairs=list(constants.SUPPORTED_PAIRS),
        timeframes=list(constants.SUPPORTED_INTERVALS),
        min_candles=_int(
            "CANDLE_WARMUP_MIN_CANDLES", constants.CANDLE_WARMUP_MIN_CANDLES
        ),
        batch_size=_int("CANDLE_WARMUP_BATCH_SIZE", constants.CANDLE_WARMUP_BATCH_SIZE),
        freshness_intervals=constants.CANDLE_WARMUP_FRESHNESS_INTERVALS,
        trim_enabled=constants.CANDLE_WARMUP_TRIM_ENABLED,
        trim_factor=constants.CANDLE_WARMUP_TRIM_FACTOR,
    )


def _ensure_aware(value: datetime) -> datetime:
    """MySQL returns naive DATETIMEs; Mongo stores UTC-aware ones."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def row_to_candle(row: dict[str, Any], timeframe: str) -> Candle | None:
    """Map one MySQL ``klines_*`` row onto the ``Candle`` model.

    Returns ``None`` when the row cannot be mapped (missing OHLCV columns or a
    non-datetime timestamp) so one malformed row cannot abort a whole
    collection's warm-up.
    """
    timestamp = row.get("timestamp") or row.get("open_time")
    if not isinstance(timestamp, datetime):
        return None

    try:
        return Candle(
            symbol=row["symbol"],
            timestamp=_ensure_aware(timestamp),
            open=row["open_price"],
            high=row["high_price"],
            low=row["low_price"],
            close=row["close_price"],
            volume=row["volume"],
            quote_volume=row.get("quote_asset_volume"),
            trades_count=row.get("number_of_trades"),
            timeframe=timeframe,
        )
    except (KeyError, TypeError, ValueError) as exc:
        logger.debug("Skipping unmappable klines row: %s", exc)
        return None


async def _trim_collection(
    mongo: MongoDBAdapter,
    collection: str,
    pair: str,
    keep: int,
    *,
    dry_run: bool,
) -> int:
    """Cap ``collection`` at the newest ``keep`` candles for ``pair``.

    Uses the existing ``query_latest``/``delete_range`` primitives: read the
    ``keep``-th newest candle's timestamp and delete everything strictly older.
    """
    if keep < 1:
        return 0

    newest = await mongo.query_latest(collection, pair, keep)
    if len(newest) < keep:
        return 0

    cutoff = newest[-1].get("timestamp")
    if not isinstance(cutoff, datetime):
        return 0
    cutoff = _ensure_aware(cutoff)

    if dry_run:
        return await mongo.get_record_count(collection, end=cutoff, symbol=pair)
    deleted = await mongo.delete_range(collection, EPOCH, cutoff, pair)
    if deleted:
        logger.info(
            "candle_warmup: trimmed %d docs older than %s from %s",
            deleted,
            cutoff.isoformat(),
            collection,
        )
    return deleted


async def backfill_pair(
    mysql: MySQLAdapter,
    mongo: MongoDBAdapter,
    pair: str,
    timeframe: str,
    config: BackfillConfig,
    *,
    now: datetime | None = None,
) -> BackfillResult:
    """Warm one ``(pair, timeframe)`` collection. Never raises."""
    coll = collection_name(pair, timeframe)
    table = mysql_table_name(timeframe)
    result = BackfillResult(
        pair=pair,
        timeframe=timeframe,
        collection=coll,
        source_table=table,
        dry_run=config.dry_run,
    )

    try:
        if not config.force:
            verdict = await evaluate_collection(
                mongo,
                pair,
                timeframe,
                required_count=config.min_candles,
                freshness_intervals=config.freshness_intervals,
                now=now,
            )
            if verdict.ready:
                result.skipped = True
                result.skip_reason = f"already warm ({verdict.count} candles, fresh)"
                logger.info("candle_warmup: %s — %s", coll, result.skip_reason)
                return result

        rows = mysql.query_latest(table, pair, config.min_candles)
        result.source_rows = len(rows)
        if not rows:
            result.skip_reason = f"no source rows in {table} for {pair}"
            logger.warning("candle_warmup: %s — %s", coll, result.skip_reason)
            return result

        candles = [c for c in (row_to_candle(r, timeframe) for r in rows) if c]
        if len(candles) != len(rows):
            logger.warning(
                "candle_warmup: %s — %d/%d source rows unmappable, skipped",
                coll,
                len(rows) - len(candles),
                len(rows),
            )
        if not candles:
            result.skip_reason = "no mappable source rows"
            return result

        if config.dry_run:
            result.written = len(candles)
            logger.info(
                "candle_warmup: %s — would write %d candles from %s (dry-run)",
                coll,
                len(candles),
                table,
            )
        else:
            await mongo.ensure_indexes(coll)
            for start in range(0, len(candles), config.batch_size):
                batch: list[Any] = list(candles[start : start + config.batch_size])
                result.written += await mongo.write(batch, coll)
            logger.info(
                "candle_warmup: %s — %d new candles written from %s (%d source rows)",
                coll,
                result.written,
                table,
                len(candles),
            )

        if config.trim_enabled:
            keep = max(config.min_candles, int(config.min_candles * config.trim_factor))
            result.trimmed = await _trim_collection(
                mongo, coll, pair, keep, dry_run=config.dry_run
            )

    except Exception as exc:
        result.error = str(exc)
        logger.error("candle_warmup: %s failed: %s", coll, exc)

    return result


async def run_backfill(
    mysql: MySQLAdapter,
    mongo: MongoDBAdapter,
    config: BackfillConfig,
    *,
    now: datetime | None = None,
) -> list[BackfillResult]:
    """Warm every ``(pair, timeframe)`` in the configured grid."""
    results: list[BackfillResult] = []
    for pair in config.pairs:
        for timeframe in config.timeframes:
            results.append(
                await backfill_pair(mysql, mongo, pair, timeframe, config, now=now)
            )

    written = sum(r.written for r in results)
    trimmed = sum(r.trimmed for r in results)
    skipped = [r for r in results if r.skipped]
    failed = [r for r in results if r.error]
    logger.info(
        "candle_warmup: run complete — %d collections, %d candles %s, "
        "%d trimmed, %d already warm, %d failed%s",
        len(results),
        written,
        "would-be-written" if config.dry_run else "written",
        trimmed,
        len(skipped),
        len(failed),
        f" ({[r.collection for r in failed]})" if failed else "",
    )
    return results


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m data_manager.maintenance.candle_warmup_backfill",
        description=(
            "Backfill the newest N candles per pair/timeframe from MySQL "
            "klines_* into Mongo candles_* ahead of the #274 primary-store "
            "flip (data-manager#275 AC1)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be written/trimmed without modifying MongoDB.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Backfill even collections that already pass the readiness gate.",
    )
    parser.add_argument(
        "--pairs", help="Comma-separated pairs. Defaults to SUPPORTED_PAIRS."
    )
    parser.add_argument(
        "--timeframes",
        help="Comma-separated timeframes. Defaults to SUPPORTED_INTERVALS.",
    )
    parser.add_argument(
        "--min-candles",
        type=int,
        default=None,
        help="Override CANDLE_WARMUP_MIN_CANDLES for this run.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="Override CANDLE_WARMUP_BATCH_SIZE for this run.",
    )
    parser.add_argument(
        "--no-trim",
        action="store_true",
        help="Disable the self-bounding capped-count trim for this run.",
    )
    return parser


def _split_csv(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [part.strip() for part in raw.split(",") if part.strip()]


def _configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )


async def _amain(argv: list[str] | None = None) -> int:
    parser = _build_argparser()
    args = parser.parse_args(argv)

    config = load_config_from_env()
    config.dry_run = args.dry_run
    config.force = args.force
    if args.pairs:
        config.pairs = _split_csv(args.pairs) or config.pairs
    if args.timeframes:
        config.timeframes = _split_csv(args.timeframes) or config.timeframes
    if args.min_candles is not None:
        config.min_candles = max(1, args.min_candles)
    if args.batch_size is not None:
        config.batch_size = max(1, args.batch_size)
    if args.no_trim:
        config.trim_enabled = False

    mongodb_url = os.getenv("MONGODB_URL")
    if not mongodb_url:
        logger.error("MONGODB_URL is not set; cannot connect to MongoDB")
        return 2

    mysql = MySQLAdapter(connection_string=constants.MYSQL_URI)
    mongo = MongoDBAdapter(connection_string=mongodb_url)
    mysql.connect()
    mongo.connect()
    try:
        results = await run_backfill(mysql, mongo, config)
    finally:
        mongo.disconnect()
        mysql.disconnect()

    return 1 if any(r.error for r in results) else 0


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    sys.exit(main())
