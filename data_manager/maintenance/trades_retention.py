"""Scheduled retention job for the ``trades`` collection (data-manager#246).

Targets ``PetroSa2/petrosa-data-manager#246`` — the root-cause fix for the 4th
MongoDB Atlas M0 quota P0 (2026-07-01), the first one driven by ``trades``
rather than ``intents`` (#244).

Why a scheduled job and not a TTL index
---------------------------------------
The ``trades`` collection holds raw public-trade ticks written **directly by the
binance-futures extractor** (data-manager does not persist individual trades —
see ``consumer/message_handler.py``). Its ``timestamp`` / ``trade_time`` /
``extracted_at`` fields are stored as **ISO-8601 strings**, not BSON ``Date``
values. A native MongoDB TTL index requires a ``Date`` field, so TTL is
impossible on the current schema — exactly the same root pattern as #244.

Instead this job deletes documents older than a configurable window via
**lexicographic string comparison** on the ISO-8601 ``timestamp`` field. ISO-8601
timestamps sort lexicographically the same way they sort chronologically (that is
the whole point of the format), so ``{"timestamp": {"$lt": cutoff_iso}}`` selects
exactly the stale rows — mirroring the manual 2026-07-01 remediation that freed
~184 MB. Deletion walks oldest-first in bounded batches so one run's blast radius
is capped and a large backlog is reclaimed over successive runs.

Invocation::

    # Dry-run — report how many docs would be deleted, mutate nothing.
    opentelemetry-instrument python -m \\
        data_manager.maintenance.trades_retention --dry-run

    # Apply — delete stale trades in bounded batches.
    opentelemetry-instrument python -m \\
        data_manager.maintenance.trades_retention --apply

Environment contract::

    MONGODB_URL                 (required) full connection string
    MONGODB_DATABASE            explicit DB name; falls back to MONGODB_DB, then
                                to "petrosa_data_manager" (never derived from the
                                connection-string path — that is how k8s#820
                                targeted the wrong DB)
    TRADES_RETENTION_DAYS       retention window in days (default 7; see constants)
    TRADES_RETENTION_COLLECTION target collection (default "trades")
    TRADES_RETENTION_TS_FIELD   ISO-8601 string field to compare (default "timestamp")
    TRADES_RETENTION_BATCH_SIZE docs deleted per batch (default 5000)
    TRADES_RETENTION_MAX_BATCHES max batches per run (default 400; bounds blast radius)

Exit codes::

    0  success (apply completed, or dry-run completed cleanly)
    2  MONGODB_URL not set
    4  MongoDB error
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

try:
    from pymongo.errors import PyMongoError
except ImportError:  # pragma: no cover - pymongo is a runtime dependency

    class PyMongoError(Exception):  # type: ignore[no-redef]
        """Fallback when pymongo is unavailable (keeps import-time safe)."""


import constants
from data_manager.db.mongodb_adapter import MongoDBAdapter

logger = logging.getLogger(__name__)

DEFAULT_DATABASE = "petrosa_data_manager"
DEFAULT_COLLECTION = "trades"
DEFAULT_TS_FIELD = "timestamp"
DEFAULT_BATCH_SIZE = 5000
DEFAULT_MAX_BATCHES = 400


@dataclass
class TradesRetentionConfig:
    """Resolved configuration for one trades-retention run."""

    database: str = DEFAULT_DATABASE
    collection: str = DEFAULT_COLLECTION
    ts_field: str = DEFAULT_TS_FIELD
    retention_days: int = constants.TRADES_RETENTION_DAYS
    batch_size: int = DEFAULT_BATCH_SIZE
    max_batches: int = DEFAULT_MAX_BATCHES
    dry_run: bool = False


@dataclass
class TradesRetentionResult:
    """Outcome of a trades-retention run."""

    database: str
    collection: str
    ts_field: str
    cutoff_iso: str
    eligible: int
    batches_processed: int
    docs_deleted: int
    capped: bool
    dry_run: bool


def resolve_database_name(environ: dict[str, str] | None = None) -> str:
    """Resolve the EXPLICIT target database name.

    Precedence: ``MONGODB_DATABASE`` → ``MONGODB_DB`` → ``petrosa_data_manager``.
    Never derived from the connection-string path, because that is exactly how
    k8s#820 ended up targeting the wrong database.
    """
    env = environ if environ is not None else os.environ
    return env.get("MONGODB_DATABASE") or env.get("MONGODB_DB") or DEFAULT_DATABASE


def compute_cutoff_iso(now: datetime, retention_days: int) -> str:
    """Return the ISO-8601 cutoff string; docs with ``ts_field`` < this are stale.

    The cutoff is rendered WITHOUT a timezone suffix (``%Y-%m-%dT%H:%M:%S``) so it
    is a clean lexicographic lower bound: a stored value such as
    ``2026-06-24T12:00:00Z`` or ``2026-06-24T12:00:00.123+00:00`` sharing the same
    second-prefix is longer than the cutoff and therefore compares GREATER — i.e.
    boundary docs are conservatively **kept**, never deleted early.
    """
    cutoff_dt = now - timedelta(days=retention_days)
    return cutoff_dt.strftime("%Y-%m-%dT%H:%M:%S")


def load_config_from_env(
    environ: dict[str, str] | None = None,
) -> TradesRetentionConfig:
    """Build a :class:`TradesRetentionConfig` from environment variables."""
    env = environ if environ is not None else os.environ
    return TradesRetentionConfig(
        database=resolve_database_name(env),
        collection=env.get("TRADES_RETENTION_COLLECTION", DEFAULT_COLLECTION),
        ts_field=env.get("TRADES_RETENTION_TS_FIELD", DEFAULT_TS_FIELD),
        retention_days=_parse_int_env(
            env, "TRADES_RETENTION_DAYS", constants.TRADES_RETENTION_DAYS, minimum=1
        ),
        batch_size=_parse_int_env(
            env, "TRADES_RETENTION_BATCH_SIZE", DEFAULT_BATCH_SIZE, minimum=1
        ),
        max_batches=_parse_int_env(
            env, "TRADES_RETENTION_MAX_BATCHES", DEFAULT_MAX_BATCHES, minimum=1
        ),
        dry_run=False,
    )


def _parse_int_env(env: dict[str, str], key: str, default: int, *, minimum: int) -> int:
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
        logger.warning("Clamping %s=%d to minimum %d", key, value, minimum)
        return minimum
    return value


async def prune_trades(
    db,
    config: TradesRetentionConfig,
    *,
    now: datetime | None = None,
) -> TradesRetentionResult:
    """Delete trades older than the retention window in bounded, oldest-first batches.

    Idempotent (AC3): each run recomputes the cutoff and deletes only what is
    currently stale; a run with nothing to delete is a clean no-op. Emits an
    auditable single-line INFO summary with the deleted count (AC3).
    """
    effective_now = now if now is not None else datetime.now(UTC)
    cutoff_iso = compute_cutoff_iso(effective_now, config.retention_days)
    coll = db[config.collection]
    stale_filter = {config.ts_field: {"$lt": cutoff_iso}}

    eligible = await coll.count_documents(stale_filter)
    if eligible == 0:
        logger.info(
            "trades_retention: db=%s collection=%s ts_field=%s cutoff=%s "
            "eligible=0 deleted=0 action=noop%s",
            config.database,
            config.collection,
            config.ts_field,
            cutoff_iso,
            " (dry-run)" if config.dry_run else "",
        )
        return TradesRetentionResult(
            database=config.database,
            collection=config.collection,
            ts_field=config.ts_field,
            cutoff_iso=cutoff_iso,
            eligible=0,
            batches_processed=0,
            docs_deleted=0,
            capped=False,
            dry_run=config.dry_run,
        )

    if config.dry_run:
        logger.info(
            "trades_retention: db=%s collection=%s ts_field=%s cutoff=%s "
            "eligible=%d deleted=0 action=dry-run",
            config.database,
            config.collection,
            config.ts_field,
            cutoff_iso,
            eligible,
        )
        return TradesRetentionResult(
            database=config.database,
            collection=config.collection,
            ts_field=config.ts_field,
            cutoff_iso=cutoff_iso,
            eligible=eligible,
            batches_processed=0,
            docs_deleted=0,
            capped=eligible > 0,
            dry_run=True,
        )

    batches_processed = 0
    docs_deleted = 0
    while batches_processed < config.max_batches:
        # Select the oldest batch of stale _ids, then delete by _id. delete_many
        # has no native limit, so this two-step keeps each batch bounded and
        # deterministic (oldest-first).
        cursor = (
            coll.find(stale_filter, {"_id": 1})
            .sort(config.ts_field, 1)
            .limit(config.batch_size)
        )
        ids = [doc["_id"] async for doc in cursor]
        if not ids:
            break
        result = await coll.delete_many({"_id": {"$in": ids}})
        deleted = result.deleted_count
        docs_deleted += deleted
        batches_processed += 1
        logger.info(
            "trades_retention: %s batch %d — deleted %d (cutoff=%s)",
            config.collection,
            batches_processed,
            deleted,
            cutoff_iso,
        )
        if deleted == 0:
            break

    remaining = await coll.count_documents(stale_filter)
    capped = remaining > 0

    logger.info(
        "trades_retention: db=%s collection=%s ts_field=%s cutoff=%s "
        "eligible=%d deleted=%d batches=%d remaining=%d capped=%s action=applied",
        config.database,
        config.collection,
        config.ts_field,
        cutoff_iso,
        eligible,
        docs_deleted,
        batches_processed,
        remaining,
        capped,
    )
    if capped:
        logger.warning(
            "trades_retention: %s hit max_batches=%d before draining backlog "
            "(%d docs still older than cutoff); next scheduled run resumes",
            config.collection,
            config.max_batches,
            remaining,
        )

    return TradesRetentionResult(
        database=config.database,
        collection=config.collection,
        ts_field=config.ts_field,
        cutoff_iso=cutoff_iso,
        eligible=eligible,
        batches_processed=batches_processed,
        docs_deleted=docs_deleted,
        capped=capped,
        dry_run=False,
    )


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m data_manager.maintenance.trades_retention",
        description=(
            "Delete trades older than the configured retention window from the "
            "trades collection via lexicographic ISO-8601 string comparison "
            "(data-manager#246)."
        ),
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Report how many docs would be deleted without mutating the database.",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Delete stale trades in bounded, oldest-first batches.",
    )
    parser.add_argument(
        "--retention-days",
        type=int,
        default=None,
        help="Override TRADES_RETENTION_DAYS for this run.",
    )
    parser.add_argument(
        "--collection",
        default=None,
        help="Override TRADES_RETENTION_COLLECTION for this run.",
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
    config.dry_run = bool(args.dry_run)
    if args.retention_days is not None:
        config.retention_days = max(1, args.retention_days)
    if args.collection:
        config.collection = args.collection

    connection_string = os.getenv("MONGODB_URL")
    if not connection_string:
        logger.error("MONGODB_URL is not set; cannot connect to MongoDB")
        return 2

    adapter = MongoDBAdapter(connection_string=connection_string)
    adapter.connect()
    try:
        # Select the database by EXPLICIT name, never the adapter's
        # connection-string-derived default (k8s#820 lesson).
        db = adapter.client[config.database]
        await prune_trades(db, config)
    except PyMongoError as exc:
        logger.error("MongoDB error during trades retention: %s", exc)
        return 4
    finally:
        adapter.disconnect()
    return 0


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    sys.exit(main())
