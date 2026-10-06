"""One-off copy of MongoDB klines into their durable MySQL history."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

import constants
from data_manager.db.database_manager import DatabaseManager
from data_manager.db.repositories.candle_repository import (
    candle_to_mysql_kline,
    map_mongo_kline_doc,
    mysql_table_name,
)
from data_manager.db.repositories.kline_persistence import (
    KLINE_NATURAL_KEY,
    kline_rows_from_documents as map_documents,
)
from data_manager.models.market_data import Candle


def _dt(value: str | datetime) -> datetime:
    parsed = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(value.replace("Z", "+00:00"))
    )
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def copy_interval(
    mongo,
    mysql,
    interval: str,
    since: datetime,
    until: datetime,
    batch: int,
    apply: bool,
) -> int:
    rows = map_documents(
        await mongo.query_range(f"klines_{interval}", since, until), interval
    )
    inserted = duplicates = failed = 0
    if apply and mysql is not None:
        for offset in range(0, len(rows), batch):
            result = mysql.write_batch(
                rows[offset : offset + batch],
                mysql_table_name(interval),
                insert_only=True,
                natural_key=KLINE_NATURAL_KEY,
            )
            inserted += result.inserted
            duplicates += result.duplicates
            failed += result.failed
        print(
            f"{interval}: {len(rows)} rows (applied) inserted={inserted} "
            f"duplicates={duplicates} failed={failed}"
        )
    else:
        print(f"{interval}: {len(rows)} rows (dry-run)")
    return len(rows)


async def run(args: argparse.Namespace) -> None:
    manager = DatabaseManager()
    await manager.initialize()
    try:
        until = _dt(args.until) if args.until else datetime.now(UTC)
        for interval in args.interval or constants.SUPPORTED_INTERVALS:
            await copy_interval(
                manager.mongodb_adapter,
                manager.mysql_adapter,
                interval,
                _dt(args.since),
                until,
                args.batch,
                args.apply,
            )
    finally:
        await manager.shutdown()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", required=True)
    parser.add_argument("--until")
    parser.add_argument(
        "--interval",
        action="append",
        choices=constants.SUPPORTED_INTERVALS,
        help="repeat to copy several; all supported intervals when omitted",
    )
    parser.add_argument("--dry-run", dest="apply", action="store_false", default=False)
    parser.add_argument("--apply", dest="apply", action="store_true")
    parser.add_argument("--batch", type=int, default=1000)
    return parser


def main() -> None:
    asyncio.run(run(_build_parser().parse_args()))


if __name__ == "__main__":
    main()
