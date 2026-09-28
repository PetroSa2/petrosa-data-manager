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
from data_manager.models.market_data import Candle


def _dt(value: str | datetime) -> datetime:
    parsed = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(value.replace("Z", "+00:00"))
    )
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def map_documents(documents: list[dict], interval: str) -> list:
    rows = []
    for document in documents:
        mapped = map_mongo_kline_doc({**document, "interval": interval})
        if mapped is None:
            continue
        try:
            candle = Candle(
                symbol=document["symbol"],
                timestamp=_dt(document["timestamp"]),
                timeframe=interval,
                open=mapped["open"],
                high=mapped["high"],
                low=mapped["low"],
                close=mapped["close"],
                volume=mapped["volume"],
                quote_volume=mapped["quote_volume"],
                trades_count=mapped["trades_count"],
            )
            rows.append(candle_to_mysql_kline(candle))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            continue
    return rows


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
    if apply and mysql is not None:
        for offset in range(0, len(rows), batch):
            mysql.write_batch(rows[offset : offset + batch], mysql_table_name(interval))
    print(f"{interval}: {len(rows)} rows ({'applied' if apply else 'dry-run'})")
    return len(rows)


async def run(args: argparse.Namespace) -> None:
    manager = DatabaseManager()
    await manager.initialize()
    try:
        until = _dt(args.until) if args.until else datetime.now(UTC)
        for interval in args.interval:
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", required=True)
    parser.add_argument("--until")
    parser.add_argument(
        "--interval",
        action="append",
        choices=constants.SUPPORTED_INTERVALS,
        default=list(constants.SUPPORTED_INTERVALS),
    )
    parser.add_argument("--dry-run", dest="apply", action="store_false", default=False)
    parser.add_argument("--apply", dest="apply", action="store_true")
    parser.add_argument("--batch", type=int, default=1000)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
