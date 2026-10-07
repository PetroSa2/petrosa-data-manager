"""Fill MongoDB ``klines_1d`` gaps from the MySQL daily lake (petrosa-data-manager#536).

The MySQL archive is nearly complete while MongoDB's ``klines_1d`` is sparse. For each symbol this compares the
UTC days present in both stores, reports the days MongoDB is missing (and the days MySQL is missing, which need
the Binance backfill), and with ``--apply`` inserts only the missing ones into MongoDB. It never changes an
existing document, and it never writes MySQL. Dry run by default; the operator runs ``--apply``.

    python -m data_manager.maintenance.fill_mongo_klines_from_mysql --since 2026-07-08
    python -m data_manager.maintenance.fill_mongo_klines_from_mysql --since 2026-07-08 --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import constants
from data_manager.db.database_manager import DatabaseManager
from data_manager.db.repositories.candle_repository import (
    MYSQL_CANDLE_COLUMNS,
    candle_to_mongo_kline,
)
from data_manager.maintenance.candle_warmup_backfill import row_to_candle
from data_manager.maintenance.klines_daily_gaps import (
    MONGO_COLLECTION,
    MYSQL_TABLE,
    day_of,
    mongo_days,
    mysql_days,
)


def _midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


async def plan_symbol(
    mongo: Any, mysql: Any, symbol: str, start: datetime, end: datetime
) -> dict[str, Any]:
    """Compare the two stores for one symbol over ``[start, end)``."""
    in_mongo = await mongo_days(mongo, symbol, start, end)
    in_mysql = await mysql_days(mysql, symbol, start, end)
    return {
        "symbol": symbol,
        "mysql_days": len(in_mysql),
        "mongo_days": len(in_mongo),
        "missing_in_mongo": sorted(in_mysql - in_mongo),
        "missing_in_mysql": sorted(in_mongo - in_mysql),
    }


async def fill_symbol(
    mongo: Any, mysql: Any, symbol: str, missing: list[date]
) -> dict[str, int]:
    """Insert the MySQL candles of the ``missing`` days into MongoDB; existing documents are never touched."""
    if not missing:
        return {"inserted": 0, "duplicates": 0, "failed": 0, "unmappable": 0}
    rows = await asyncio.to_thread(
        mysql.query_range,
        MYSQL_TABLE,
        _midnight(min(missing)),
        _midnight(max(missing)) + timedelta(days=1),
        symbol,
        columns=MYSQL_CANDLE_COLUMNS
        + ("open_time", "quote_asset_volume", "number_of_trades"),
    )
    wanted = set(missing)
    candles = [
        candle
        for row in rows
        if day_of(row["timestamp"]) in wanted
        and (candle := row_to_candle(row, "1d")) is not None
    ]
    result = await mongo.write(
        [candle_to_mongo_kline(candle) for candle in candles], MONGO_COLLECTION
    )
    return {
        "inserted": int(result.inserted),
        "duplicates": int(result.duplicates),
        "failed": int(result.failed),
        "unmappable": len(wanted) - len(candles),
    }


async def run(args: argparse.Namespace) -> list[dict[str, Any]]:
    manager = DatabaseManager()
    await manager.initialize()
    try:
        today = datetime.now(UTC).date()
        start = (
            datetime.fromisoformat(args.since).astimezone(UTC)
            if args.since
            else (_midnight(today) - timedelta(days=90))
        )
        end = (
            datetime.fromisoformat(args.until).astimezone(UTC)
            if args.until
            else _midnight(today)
        )
        reports = []
        for symbol in args.symbol or constants.SUPPORTED_PAIRS:
            report = await plan_symbol(
                manager.mongodb_adapter, manager.mysql_adapter, symbol, start, end
            )
            if args.apply:
                report["applied"] = await fill_symbol(
                    manager.mongodb_adapter,
                    manager.mysql_adapter,
                    symbol,
                    report["missing_in_mongo"],
                )
            reports.append(report)
        return reports
    finally:
        await manager.shutdown()


def _line(report: dict[str, Any]) -> str:
    days = ",".join(day.isoformat() for day in report["missing_in_mongo"]) or "-"
    gaps = ",".join(day.isoformat() for day in report["missing_in_mysql"]) or "-"
    line = (
        f"{report['symbol']}: mysql={report['mysql_days']} mongo={report['mongo_days']} "
        f"missing_in_mongo={len(report['missing_in_mongo'])} [{days}] "
        f"missing_in_mysql={len(report['missing_in_mysql'])} [{gaps}]"
    )
    if "applied" in report:
        applied = report["applied"]
        line += (
            f" applied: inserted={applied['inserted']} duplicates={applied['duplicates']} "
            f"failed={applied['failed']} unmappable={applied['unmappable']}"
        )
    return line


def _failed(reports: list[dict[str, Any]]) -> bool:
    return any(
        report["applied"]["failed"] or report["applied"]["unmappable"]
        for report in reports
        if "applied" in report
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", help="first day (ISO, UTC); default 90 days ago")
    parser.add_argument(
        "--until", help="end, exclusive (ISO, UTC); default today 00:00"
    )
    parser.add_argument(
        "--symbol",
        action="append",
        help="repeat for several; all supported when omitted",
    )
    parser.add_argument("--dry-run", dest="apply", action="store_false", default=False)
    parser.add_argument("--apply", dest="apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    reports = asyncio.run(run(args))
    for report in reports:
        print(_line(report))
    print(json.dumps(reports, default=str, sort_keys=True))
    return 1 if args.apply and _failed(reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())
