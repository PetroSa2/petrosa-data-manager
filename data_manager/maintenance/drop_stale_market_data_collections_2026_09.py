"""Guarded drop migration for the 2026-09-02 stale per-symbol market-data audit.

Targets AC2/AC3 of `PetroSa2/petrosa-data-manager#273` (follow-up in spirit to
#272 / PR #288's `drop_orphan_petrosa_crypto_tables_2026_09.py`, which this
module mirrors: guarded, `--dry-run`/`--apply`, idempotent, per-target
independently guarded, never fatal to the batch).

The full AC1 evidence trail is on the issue itself (2026-09-13 "Adversarial
Review Corrections" comment). Summary of the three target categories in the
live `petrosa_data_manager` Mongo DB:

* ``tickers_{symbol}`` (e.g. ``tickers_BTCUSDT``) — **confirmed dead, no
  reader**. ``TickerRepository`` is never instantiated by any API route
  (grep confirmed; only ``TradeRepository`` is wired in
  ``data_manager/api/routes/data.py``). Writer dormant since 2025-10-21
  (``message_handler.py`` does not persist tickers). Safe default drop
  target.
* ``trades_{symbol}`` (e.g. ``trades_BTCUSDT``) — **wired-but-empty reader,
  no writer** (per the issue's adversarial-review correction). ``GET
  /api/v1/data/trades`` (``data.py:279``) reads this collection via
  ``TradeRepository.get_range``. This is the *same category* #272 used to
  justify *retaining* ``datasets``/``lineage_records`` — a live reader
  exists even though the data is stale and the writer is dormant. Retained
  by default; only processed with the explicit ``--include-wired-reader``
  flag, and even then still subject to the same recency guard as every
  other target.
* ``trades`` (plain, no per-symbol suffix) — **retirement already decided**.
  ``data_manager/maintenance/intents_ttl_index.py``'s ``SIBLING_COLLECTIONS``
  history records: "the `trades` collection is being retired entirely
  (binance-data-extractor#276), so it now falls back to the default sibling
  decision until the collection is dropped" (the retention-job override for
  it was removed in data-manager#254). Default drop target.

GUARD (recency, not row-count): unlike #272's MySQL tables — which were
verified 0-row orphans — these Mongo collections hold real historical
documents. The safety guard here is **recency**, not emptiness: a target is
only dropped in ``--apply`` mode if its newest document is older than
``--min-age-days`` (default 30, env ``MARKET_DATA_STALE_MIN_AGE_DAYS``). A
collection that received a write more recently than the guard window means
some writer reactivated — the drop is skipped (not fatal to the batch) and
reported so operators can re-audit, exactly mirroring #288's "guard tripped,
skip, not fatal" behavior for the row-count case.

Operator invocation:

    # Dry-run: report every target collection (doc count, newest-doc age,
    # category) without dropping anything.
    opentelemetry-instrument python -m \\
        data_manager.maintenance.drop_stale_market_data_collections_2026_09 --dry-run

    # Apply: drops tickers_{symbol} and plain `trades` collections that pass
    # the recency guard. trades_{symbol} (wired reader) is NOT touched unless
    # --include-wired-reader is also passed.
    opentelemetry-instrument python -m \\
        data_manager.maintenance.drop_stale_market_data_collections_2026_09 --apply

    # Restrict to specific collections (repeatable), e.g. for a staged rollout:
    ... --apply --collection tickers_BTCUSDT --collection tickers_ETHUSDT

    # Opt in to also processing the wired-reader trades_{symbol} category
    # (operator has confirmed the API route consumer accepts empty results,
    # or has separately deprecated GET /api/v1/data/trades):
    ... --apply --include-wired-reader

Exit codes:
    0  — success (all requested collections processed; guard trips are
         reported but do not fail the run in --dry-run mode)
    2  — MONGODB_URL not set
    3  — --apply mode and at least one requested collection tripped the
         recency guard (newest doc younger than --min-age-days); collections
         that passed the guard were still dropped
    4  — database error
    5  — invocation error (unknown --collection, etc.)

Every drop is a plain ``drop()`` on the Motor collection handle — idempotent;
re-running after a successful drop (or against a collection already absent)
is a no-op.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

from data_manager.db.mongodb_adapter import MongoDBAdapter

logger = logging.getLogger(__name__)

TICKERS_PREFIX = "tickers_"
TRADES_PREFIX = "trades_"
PLAIN_TRADES_COLLECTION = "trades"

CATEGORY_TICKERS_SYMBOL = "tickers_symbol"
CATEGORY_TRADES_SYMBOL = "trades_symbol"
CATEGORY_TRADES_PLAIN = "trades_plain"

# Categories processed by default in --apply mode. trades_symbol (wired
# reader) requires the explicit --include-wired-reader opt-in (AC1 correction
# precedent: #272 retained wired-but-empty-reader tables like `datasets`).
DEFAULT_APPLY_CATEGORIES = (CATEGORY_TICKERS_SYMBOL, CATEGORY_TRADES_PLAIN)

DEFAULT_MIN_AGE_DAYS = 30


@dataclass
class CollectionDropResult:
    """Per-collection outcome of a stale-market-data drop run."""

    collection: str
    category: str | None
    dry_run: bool
    existed: bool = False
    doc_count: int = 0
    newest_doc_age_days: float | None = None
    guard_tripped: bool = False
    retained_wired_reader: bool = False
    dropped: bool = False


def classify_collection(name: str) -> str | None:
    """Classify `name` into one of the three target categories, or None.

    ``tickers_`` / ``trades_`` require a non-empty symbol suffix so bare
    prefix artifacts (there are none in practice, but defensively) are not
    misclassified.
    """
    if name == PLAIN_TRADES_COLLECTION:
        return CATEGORY_TRADES_PLAIN
    if name.startswith(TICKERS_PREFIX) and len(name) > len(TICKERS_PREFIX):
        return CATEGORY_TICKERS_SYMBOL
    if name.startswith(TRADES_PREFIX) and len(name) > len(TRADES_PREFIX):
        return CATEGORY_TRADES_SYMBOL
    return None


async def discover_target_collections(adapter: MongoDBAdapter) -> list[str]:
    """Return every collection in the DB that classifies into a target
    category, sorted for deterministic output."""
    collections = await adapter.list_collections()
    return sorted(c for c in collections if classify_collection(c) is not None)


async def _newest_doc_age_days(
    adapter: MongoDBAdapter, collection: str, *, now: datetime
) -> float | None:
    """Return the age in days of the newest document in `collection`, or
    None if the collection is empty / has no usable timestamp."""
    latest = await adapter.query_latest(collection, limit=1)
    if not latest:
        return None
    ts = latest[0].get("timestamp")
    if not isinstance(ts, datetime):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (now - ts).total_seconds() / 86400.0


async def process_collection(
    adapter: MongoDBAdapter,
    collection: str,
    *,
    dry_run: bool,
    min_age_days: int,
    include_wired_reader: bool,
    now: datetime | None = None,
) -> CollectionDropResult:
    """Run the guarded-drop flow for a single collection. Never raises —
    backend errors and guard trips are reported in the result; the caller
    decides whether that constitutes a failing exit code."""
    now = now if now is not None else datetime.now(UTC)
    category = classify_collection(collection)
    result = CollectionDropResult(
        collection=collection, category=category, dry_run=dry_run
    )

    if category == CATEGORY_TRADES_SYMBOL and not include_wired_reader:
        result.retained_wired_reader = True
        logger.info(
            "%s: %s retained — wired reader (GET /api/v1/data/trades), "
            "pass --include-wired-reader to process it",
            __name__,
            collection,
        )
        return result

    try:
        doc_count = await adapter.get_record_count(collection)
    except Exception as e:  # noqa: BLE001 — never abort the batch
        logger.error("%s: %s — count failed: %s", __name__, collection, e)
        return result

    if doc_count == 0:
        # Collection absent or already empty; nothing to guard against.
        try:
            names = await adapter.list_collections()
        except Exception:  # noqa: BLE001
            names = []
        result.existed = collection in names
        if not result.existed:
            logger.info("%s: %s not present — nothing to do", __name__, collection)
            return result
    else:
        result.existed = True
    result.doc_count = doc_count

    try:
        age_days = await _newest_doc_age_days(adapter, collection, now=now)
    except Exception as e:  # noqa: BLE001
        logger.error(
            "%s: %s — newest-doc lookup failed, refusing to drop: %s",
            __name__,
            collection,
            e,
        )
        result.guard_tripped = True
        return result
    result.newest_doc_age_days = age_days

    if age_days is None or age_days < min_age_days:
        result.guard_tripped = True
        msg = (
            "%s: dry-run sees %s (age=%s days) younger than guard window "
            "(%d days) — drop would be refused"
            if dry_run
            else "%s: refusing to drop %s: recency guard tripped "
            "(newest doc age=%s days < %d) — the stale classification is "
            "no longer valid for this collection"
        )
        log_fn = logger.warning if dry_run else logger.error
        log_fn(msg, __name__, collection, age_days, min_age_days)
        return result

    if dry_run:
        logger.info(
            "%s: dry-run — would drop %s (docs=%d, newest-doc age=%.1f days)",
            __name__,
            collection,
            doc_count,
            age_days,
        )
        return result

    await adapter.db[collection].drop()
    result.dropped = True
    logger.info(
        "%s: dropped %s (%d docs, newest-doc age=%.1f days reclaimed)",
        __name__,
        collection,
        doc_count,
        age_days,
    )
    return result


async def execute_migration(
    adapter: MongoDBAdapter,
    *,
    dry_run: bool,
    min_age_days: int = DEFAULT_MIN_AGE_DAYS,
    include_wired_reader: bool = False,
    collections: list[str] | None = None,
) -> list[CollectionDropResult]:
    """Run the guarded-drop flow across every target collection.

    `collections`, when given, restricts processing to that explicit list
    (still classified/guarded per-item) instead of the auto-discovered set.
    """
    targets = (
        list(collections)
        if collections is not None
        else await discover_target_collections(adapter)
    )
    return [
        await process_collection(
            adapter,
            name,
            dry_run=dry_run,
            min_age_days=min_age_days,
            include_wired_reader=include_wired_reader,
        )
        for name in targets
    ]


def _resolve_min_age_days(cli_value: int | None) -> int:
    if cli_value is not None:
        if cli_value < 0:
            logger.warning("Clamping --min-age-days=%d to minimum 0", cli_value)
            return 0
        return cli_value
    raw = os.getenv("MARKET_DATA_STALE_MIN_AGE_DAYS")
    if raw is None:
        return DEFAULT_MIN_AGE_DAYS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Ignoring non-integer MARKET_DATA_STALE_MIN_AGE_DAYS=%r; using default %d",
            raw,
            DEFAULT_MIN_AGE_DAYS,
        )
        return DEFAULT_MIN_AGE_DAYS
    if value < 0:
        logger.warning("Clamping MARKET_DATA_STALE_MIN_AGE_DAYS=%d to minimum 0", value)
        return 0
    return value


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m data_manager.maintenance.drop_stale_market_data_collections_2026_09",
        description=(
            "Guarded drop of confirmed-stale trades_{symbol}/tickers_{symbol} "
            "(and plain `trades`) Mongo collections. See "
            "data-manager#273 for the AC1 evidence."
        ),
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Show doc count + newest-doc age per collection without dropping anything.",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Drop every target collection that passes the recency guard.",
    )
    parser.add_argument(
        "--collection",
        action="append",
        dest="collections",
        default=None,
        help=(
            "Restrict to this collection (repeatable). Default: auto-discover "
            "every tickers_*/trades_*/trades collection in the database."
        ),
    )
    parser.add_argument(
        "--min-age-days",
        type=int,
        default=None,
        help=(
            "Override MARKET_DATA_STALE_MIN_AGE_DAYS for this run "
            f"(default {DEFAULT_MIN_AGE_DAYS})."
        ),
    )
    parser.add_argument(
        "--include-wired-reader",
        action="store_true",
        help=(
            "Also process trades_{symbol} collections (wired reader via "
            "GET /api/v1/data/trades). Off by default per the AC1 "
            "wired-but-empty-reader precedent (#272's `datasets` retention)."
        ),
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

    connection_string = os.getenv("MONGODB_URL")
    if not connection_string:
        logger.error("MONGODB_URL is not set; cannot connect to MongoDB")
        return 2

    min_age_days = _resolve_min_age_days(args.min_age_days)

    adapter = MongoDBAdapter(connection_string=connection_string)
    adapter.connect()
    try:
        results = await execute_migration(
            adapter,
            dry_run=args.dry_run,
            min_age_days=min_age_days,
            include_wired_reader=args.include_wired_reader,
            collections=args.collections,
        )
    except Exception as exc:  # noqa: BLE001 — surface as a database error
        logger.error("database error during migration: %s", exc)
        return 4
    finally:
        adapter.disconnect()

    dropped = [r.collection for r in results if r.dropped]
    guarded = [r.collection for r in results if r.guard_tripped]
    retained = [r.collection for r in results if r.retained_wired_reader]
    logger.info(
        "migration summary: dropped=%s guarded(recency, skipped)=%s "
        "retained(wired-reader)=%s",
        dropped,
        guarded,
        retained,
    )

    if not args.dry_run and guarded:
        return 3

    return 0


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    sys.exit(main())
