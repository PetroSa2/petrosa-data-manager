"""Prometheus completeness metric for the permanent MySQL kline archive."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from prometheus_client import Gauge

from data_manager.db.repositories.candle_repository import mysql_table_name
from data_manager.utils.time_utils import parse_timeframe_to_minutes

MYSQL_KLINES_COMPLETENESS = Gauge(
    "data_manager_mysql_klines_completeness_ratio",
    "Expected versus present historic MySQL klines",
    ["symbol", "timeframe"],
)


def completeness_ratio(
    present: int, start: datetime, end: datetime, timeframe: str
) -> float:
    """Return the bounded [0, 1] ratio for a regular candle series."""
    minutes = parse_timeframe_to_minutes(timeframe)
    expected = max(0, int((end - start).total_seconds() // (minutes * 60)))
    return min(1.0, present / expected) if expected else 1.0


def update_completeness(
    mysql: Any, symbol: str, timeframe: str, start: datetime, end: datetime
) -> float:
    """Calculate and publish one symbol/timeframe archive ratio."""
    present = int(
        mysql.get_record_count(mysql_table_name(timeframe), start, end, symbol)
    )
    ratio = completeness_ratio(present, start, end, timeframe)
    MYSQL_KLINES_COMPLETENESS.labels(symbol=symbol, timeframe=timeframe).set(ratio)
    return ratio
