"""
Mongo candle readiness gate (AC2 of petrosa-data-manager#275).

Answers one question, fail-closed: *is it safe to promote MongoDB to the
primary execution candle store yet?*

A ``candles_{pair}_{timeframe}`` collection is **ready** when both hold:

1. **Depth** — it holds at least ``CANDLE_WARMUP_MIN_CANDLES`` candles for the
   pair (400 by default, the contract from
   ``docs/candle-consumer-retention-contract.md`` / #276: true max strategy
   lookback 265 x 1.5 safety margin).
2. **Freshness** — its newest candle is no older than
   ``CANDLE_WARMUP_FRESHNESS_INTERVALS`` x the collection's own timeframe, so a
   deep-but-stale collection (backfilled once, never kept current) cannot pass.

Every failure mode — missing collection, unreachable database, unparseable
timeframe, exception mid-query — resolves to **not ready**. The gate never
fails open: a cutover that cannot be *proved* safe is blocked.

Usage::

    python -m data_manager.maintenance.candle_readiness [--json]

Exit codes: ``0`` every pair/timeframe ready, ``1`` at least one not ready
(the #274 primary-store flip must not proceed), ``2`` invocation/connection
error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

import constants
from data_manager.db.mongodb_adapter import MongoDBAdapter
from data_manager.db.repositories.candle_repository import mongo_collection_name
from data_manager.utils.time_utils import parse_timeframe_to_seconds

logger = logging.getLogger(__name__)

# Single source of truth for collection naming lives in the repository; the
# maintenance jobs re-export it so they can resolve names without constructing
# a repository (which requires live adapters).
collection_name = mongo_collection_name


@dataclass
class CollectionReadiness:
    """Readiness verdict for a single ``(pair, timeframe)`` collection."""

    pair: str
    timeframe: str
    collection: str
    count: int
    required_count: int
    newest_timestamp: str | None
    age_seconds: float | None
    max_age_seconds: float | None
    ready: bool
    reasons: list[str] = field(default_factory=list)


@dataclass
class ReadinessReport:
    """Aggregate verdict across the whole execution grid."""

    ready: bool
    checked_at: str
    required_count: int
    freshness_intervals: float
    collections: list[CollectionReadiness]

    @property
    def not_ready(self) -> list[CollectionReadiness]:
        return [c for c in self.collections if not c.ready]

    def to_dict(self) -> dict:
        return {
            "ready": self.ready,
            "checked_at": self.checked_at,
            "required_count": self.required_count,
            "freshness_intervals": self.freshness_intervals,
            "collections": [asdict(c) for c in self.collections],
        }


def _ensure_aware(value: datetime) -> datetime:
    """Return ``value`` with UTC tzinfo when it was stored naive."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _max_age_seconds(timeframe: str, freshness_intervals: float) -> float | None:
    """Return the freshness budget for ``timeframe``, or ``None`` if unparseable."""
    try:
        return parse_timeframe_to_seconds(timeframe) * freshness_intervals
    except (ValueError, TypeError):
        return None


async def evaluate_collection(
    adapter: MongoDBAdapter,
    pair: str,
    timeframe: str,
    *,
    required_count: int,
    freshness_intervals: float,
    now: datetime | None = None,
) -> CollectionReadiness:
    """Evaluate one ``(pair, timeframe)`` collection. Never raises."""
    effective_now = now if now is not None else datetime.now(UTC)
    coll = collection_name(pair, timeframe)
    max_age = _max_age_seconds(timeframe, freshness_intervals)

    verdict = CollectionReadiness(
        pair=pair,
        timeframe=timeframe,
        collection=coll,
        count=0,
        required_count=required_count,
        newest_timestamp=None,
        age_seconds=None,
        max_age_seconds=max_age,
        ready=False,
    )

    if max_age is None:
        verdict.reasons.append(f"unparseable timeframe {timeframe!r}")
        return verdict

    try:
        verdict.count = await adapter.get_record_count(coll, symbol=pair)
        latest = await adapter.query_latest(coll, pair, 1)
    except Exception as exc:  # fail closed on any backend error
        verdict.reasons.append(f"query failed: {exc}")
        return verdict

    if verdict.count < required_count:
        verdict.reasons.append(f"depth {verdict.count} < required {required_count}")

    if not latest:
        verdict.reasons.append("no candles present")
        return verdict

    newest = latest[0].get("timestamp")
    if not isinstance(newest, datetime):
        verdict.reasons.append(f"newest candle has non-datetime timestamp {newest!r}")
        return verdict

    newest = _ensure_aware(newest)
    verdict.newest_timestamp = newest.isoformat()
    verdict.age_seconds = (effective_now - newest).total_seconds()

    if verdict.age_seconds > max_age:
        verdict.reasons.append(
            f"newest candle is {verdict.age_seconds:.0f}s old, budget {max_age:.0f}s"
        )

    verdict.ready = not verdict.reasons
    return verdict


async def evaluate_readiness(
    adapter: MongoDBAdapter,
    *,
    pairs: list[str] | None = None,
    timeframes: list[str] | None = None,
    required_count: int | None = None,
    freshness_intervals: float | None = None,
    now: datetime | None = None,
) -> ReadinessReport:
    """Evaluate the whole ``pairs x timeframes`` execution grid, fail-closed."""
    effective_now = now if now is not None else datetime.now(UTC)
    effective_pairs = pairs if pairs is not None else list(constants.SUPPORTED_PAIRS)
    effective_timeframes = (
        timeframes if timeframes is not None else list(constants.SUPPORTED_INTERVALS)
    )
    required = (
        required_count
        if required_count is not None
        else constants.CANDLE_WARMUP_MIN_CANDLES
    )
    freshness = (
        freshness_intervals
        if freshness_intervals is not None
        else constants.CANDLE_WARMUP_FRESHNESS_INTERVALS
    )

    results: list[CollectionReadiness] = []
    for pair in effective_pairs:
        for timeframe in effective_timeframes:
            results.append(
                await evaluate_collection(
                    adapter,
                    pair,
                    timeframe,
                    required_count=required,
                    freshness_intervals=freshness,
                    now=effective_now,
                )
            )

    # Fail closed: an empty grid is NOT a pass. "Nothing checked" must never
    # be mistaken for "everything verified".
    overall = bool(results) and all(r.ready for r in results)

    report = ReadinessReport(
        ready=overall,
        checked_at=effective_now.isoformat(),
        required_count=required,
        freshness_intervals=freshness,
        collections=results,
    )

    if overall:
        logger.info(
            "candle_readiness: READY — %d collections meet depth>=%d and freshness",
            len(results),
            required,
        )
    else:
        logger.warning(
            "candle_readiness: NOT READY — %d/%d collections failing: %s",
            len(report.not_ready),
            len(results) or 1,
            ", ".join(
                f"{c.collection}({'; '.join(c.reasons)})" for c in report.not_ready[:10]
            )
            or "empty grid",
        )
    return report


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m data_manager.maintenance.candle_readiness",
        description=(
            "Fail-closed readiness gate for promoting MongoDB to the primary "
            "execution candle store (data-manager#275 AC2)."
        ),
    )
    parser.add_argument(
        "--pairs", help="Comma-separated pairs to check. Defaults to SUPPORTED_PAIRS."
    )
    parser.add_argument(
        "--timeframes",
        help="Comma-separated timeframes to check. Defaults to SUPPORTED_INTERVALS.",
    )
    parser.add_argument(
        "--min-candles",
        type=int,
        default=None,
        help="Override CANDLE_WARMUP_MIN_CANDLES for this run.",
    )
    parser.add_argument(
        "--freshness-intervals",
        type=float,
        default=None,
        help="Override CANDLE_WARMUP_FRESHNESS_INTERVALS for this run.",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit the full report as JSON on stdout."
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

    connection_string = os.getenv("MONGODB_URL")
    if not connection_string:
        logger.error("MONGODB_URL is not set; cannot connect to MongoDB")
        return 2

    adapter = MongoDBAdapter(connection_string=connection_string)
    adapter.connect()
    try:
        report = await evaluate_readiness(
            adapter,
            pairs=_split_csv(args.pairs),
            timeframes=_split_csv(args.timeframes),
            required_count=args.min_candles,
            freshness_intervals=args.freshness_intervals,
        )
    finally:
        adapter.disconnect()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))

    return 0 if report.ready else 1


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    sys.exit(main())
