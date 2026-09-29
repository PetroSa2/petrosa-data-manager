"""Resumable, idempotent Binance-to-MySQL historic kline backfill."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import constants
from data_manager.backfiller.binance_client import BinanceClient
from data_manager.db.mysql_adapter import MySQLAdapter
from data_manager.db.repositories.candle_repository import candle_to_mysql_kline
from data_manager.models.market_data import Candle
from data_manager.utils.time_utils import parse_timeframe_to_minutes

logger = logging.getLogger(__name__)

DEFAULT_START = datetime(2026, 9, 24, tzinfo=UTC)
EARLY_DAILY_START = datetime(2021, 1, 1, tzinfo=UTC)
DAILY_GAP_END = datetime(2021, 9, 18, tzinfo=UTC)


@dataclass
class BackfillConfig:
    """Bounds and persistence settings for a backfill run."""

    symbols: list[str]
    timeframes: list[str]
    start: datetime = DEFAULT_START
    end: datetime | None = None
    daily_start: datetime = EARLY_DAILY_START
    daily_end: datetime = DAILY_GAP_END
    batch_size: int = 1000
    dry_run: bool = True
    checkpoint: Path | None = None


def _candle_from_binance(symbol: str, timeframe: str, row: list[Any]) -> Candle:
    return Candle(
        symbol=symbol,
        timestamp=datetime.fromtimestamp(float(row[0]) / 1000, tz=UTC),
        open=str(row[1]),
        high=str(row[2]),
        low=str(row[3]),
        close=str(row[4]),
        volume=str(row[5]),
        quote_volume=str(row[7]),
        trades_count=int(row[8]),
        timeframe=timeframe,
        taker_buy_base_volume=str(row[9]),
        taker_buy_quote_volume=str(row[10]),
    )


def _load_checkpoint(path: Path | None) -> set[str]:
    if path is None or not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text()))
    except (OSError, TypeError, ValueError):
        logger.warning("Ignoring invalid backfill checkpoint %s", path)
        return set()


def _save_checkpoint(path: Path | None, completed: set[str]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(completed), indent=2) + "\n")


def _ranges(config: BackfillConfig) -> list[tuple[str, str, datetime, datetime]]:
    end = config.end or datetime.now(UTC)
    ranges = [
        (symbol, timeframe, config.start, end)
        for symbol in config.symbols
        for timeframe in config.timeframes
    ]
    if "1d" in config.timeframes:
        ranges.extend(
            (symbol, "1d", config.daily_start, config.daily_end)
            for symbol in config.symbols
        )
    return ranges


async def run_backfill(
    client: Any, mysql: Any, config: BackfillConfig
) -> dict[str, int]:
    """Fetch and persist all ranges, returning fetched/inserted counters."""
    completed = _load_checkpoint(config.checkpoint)
    fetched = inserted = 0
    for symbol, timeframe, start, end in _ranges(config):
        key = f"{symbol}:{timeframe}:{start.isoformat()}:{end.isoformat()}"
        if key in completed:
            continue
        cursor = start
        step = timedelta(minutes=parse_timeframe_to_minutes(timeframe) * 1000)
        while cursor < end:
            chunk_end = min(cursor + step, end)
            rows = await client.get_klines(
                symbol, timeframe, cursor, chunk_end, limit=config.batch_size
            )
            if not rows:
                cursor = chunk_end
                continue
            candles = [_candle_from_binance(symbol, timeframe, row) for row in rows]
            fetched += len(candles)
            if not config.dry_run:
                result = await asyncio.to_thread(
                    mysql.write_batch,
                    [candle_to_mysql_kline(candle) for candle in candles],
                    f"klines_{timeframe[-1]}{timeframe[:-1]}",
                    config.batch_size,
                )
                inserted += int(getattr(result, "inserted", result))
            last = datetime.fromtimestamp(float(rows[-1][0]) / 1000, tz=UTC)
            cursor = max(
                chunk_end,
                last + timedelta(minutes=parse_timeframe_to_minutes(timeframe)),
            )
        completed.add(key)
        _save_checkpoint(config.checkpoint, completed)
    return {"fetched": fetched, "inserted": inserted, "dry_run": int(config.dry_run)}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default=",".join(constants.SUPPORTED_PAIRS))
    parser.add_argument("--timeframes", default=",".join(constants.SUPPORTED_INTERVALS))
    parser.add_argument("--start", default=DEFAULT_START.isoformat())
    parser.add_argument("--end")
    parser.add_argument(
        "--checkpoint", type=Path, default=Path(".tmp/klines-backfill.json")
    )
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args(argv)


async def _amain(args: argparse.Namespace) -> int:
    config = BackfillConfig(
        symbols=[x.strip() for x in args.symbols.split(",") if x.strip()],
        timeframes=[x.strip() for x in args.timeframes.split(",") if x.strip()],
        start=datetime.fromisoformat(args.start).astimezone(UTC),
        end=datetime.fromisoformat(args.end).astimezone(UTC) if args.end else None,
        batch_size=max(1, args.batch_size),
        dry_run=not args.apply,
        checkpoint=args.checkpoint,
    )
    client = BinanceClient()
    mysql = MySQLAdapter(connection_string=constants.MYSQL_URI)
    mysql.connect()
    try:
        print(await run_backfill(client, mysql, config))
    finally:
        await client.close()
        mysql.disconnect()
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(_parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
