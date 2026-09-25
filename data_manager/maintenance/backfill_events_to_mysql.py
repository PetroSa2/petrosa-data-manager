"""Backfill MongoDB audit events into the durable MySQL archive.

The command is intentionally dry-run by default.  Use ``--apply`` only after
reviewing the reported counts; MySQL writes are idempotent on ``event_key``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Callable
from typing import Any

import constants
from data_manager.db.mongodb_adapter import MongoDBAdapter
from data_manager.db.mysql_adapter import MySQLAdapter
from data_manager.models.execution_event import ExecutionEvent
from data_manager.models.pnl_event import PnlEvent

logger = logging.getLogger(__name__)

EVENT_SPECS: tuple[tuple[str, type[Any]], ...] = (
    ("execution_events", ExecutionEvent),
    ("pnl_events", PnlEvent),
)


async def backfill_collection(
    mongo_collection: Any,
    mysql_adapter: Any,
    collection: str,
    model_factory: Callable[..., Any],
    *,
    batch_size: int = 500,
    dry_run: bool = True,
) -> int:
    """Copy one Mongo collection in bounded batches and return valid count."""
    cursor = mongo_collection.find({})
    total = 0
    while True:
        documents = await cursor.to_list(length=batch_size)
        if not documents:
            break
        models = []
        for document in documents:
            document = dict(document)
            document.pop("_id", None)
            try:
                models.append(model_factory(**document))
            except Exception:
                logger.warning(
                    "Skipping invalid %s document during backfill",
                    collection,
                    exc_info=True,
                )
        total += len(models)
        if models and not dry_run:
            mysql_adapter.write(models, collection)
    return total


async def run_backfill(
    mongo_adapter: Any,
    mysql_adapter: Any,
    *,
    batch_size: int = 500,
    dry_run: bool = True,
) -> dict[str, int]:
    """Backfill both event streams and return per-collection counts."""
    counts: dict[str, int] = {}
    for collection, model_factory in EVENT_SPECS:
        counts[collection] = await backfill_collection(
            mongo_adapter.db[collection],
            mysql_adapter,
            collection,
            model_factory,
            batch_size=batch_size,
            dry_run=dry_run,
        )
    return counts


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write rows to MySQL")
    parser.add_argument("--batch-size", type=int, default=500)
    return parser.parse_args(argv)


async def _amain(args: argparse.Namespace) -> int:
    if args.batch_size < 1:
        raise ValueError("--batch-size must be positive")
    mongo = MongoDBAdapter(connection_string=constants.MONGODB_URL)
    mysql = MySQLAdapter(connection_string=constants.MYSQL_URI)
    mongo.connect()
    mysql.connect()
    try:
        counts = await run_backfill(
            mongo,
            mysql,
            batch_size=args.batch_size,
            dry_run=not args.apply,
        )
        mode = "apply" if args.apply else "dry-run"
        print(
            f"mode={mode} execution_events={counts['execution_events']} pnl_events={counts['pnl_events']}"
        )
        return 0
    finally:
        mongo.disconnect()
        mysql.disconnect()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    return asyncio.run(_amain(_parse_args(argv)))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
