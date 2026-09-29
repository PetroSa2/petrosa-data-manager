"""Validation and observability helpers for stored candles."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from prometheus_client import Counter

from data_manager.utils.time_utils import as_aware_utc, parse_timeframe_to_seconds

CANDLE_SANITY_VIOLATIONS = Counter(
    "data_manager_candle_sanity_violations_total",
    "Candles rejected by a data-sanity sentinel",
    ["symbol", "timeframe", "reason"],
)


def validate_candle_document(
    document: dict[str, Any], *, now: datetime | None = None
) -> list[str]:
    """Return all sentinel failures for an extractor-format candle document."""
    symbol = str(document.get("symbol") or "unknown")
    timeframe = str(document.get("interval") or "unknown")
    failures: list[str] = []

    try:
        timestamp = as_aware_utc(document["timestamp"])
    except (KeyError, TypeError, ValueError):
        failures.append("invalid_timestamp")
        timestamp = None

    values: dict[str, Decimal] = {}
    for field in ("open_price", "high_price", "low_price", "close_price", "volume"):
        try:
            values[field] = Decimal(str(document[field]))
        except (KeyError, TypeError, ValueError, InvalidOperation):
            failures.append(f"invalid_{field}")

    prices = [
        values.get(field)
        for field in ("open_price", "high_price", "low_price", "close_price")
    ]
    if any(
        value is not None and (not value.is_finite() or value <= 0) for value in prices
    ):
        failures.append("non_positive_price")
    if all(value is not None for value in prices):
        low = values["low_price"]
        high = values["high_price"]
        if low > values["open_price"] or low > values["close_price"]:
            failures.append("low_above_price")
        if high < values["open_price"] or high < values["close_price"]:
            failures.append("high_below_price")
    volume = values.get("volume")
    if volume is not None and (not volume.is_finite() or volume < 0):
        failures.append("negative_volume")

    if timestamp is not None:
        try:
            interval = parse_timeframe_to_seconds(timeframe)
            if int(timestamp.timestamp()) % interval:
                failures.append("unaligned_timestamp")
        except (TypeError, ValueError, ZeroDivisionError):
            failures.append("invalid_timeframe")
        if timestamp > as_aware_utc(now or datetime.now(UTC)):
            failures.append("future_timestamp")

    for reason in failures:
        CANDLE_SANITY_VIOLATIONS.labels(
            symbol=symbol, timeframe=timeframe, reason=reason
        ).inc()
    return failures


def validate_candle_batch(documents: list[dict[str, Any]]) -> dict[int, list[str]]:
    """Validate documents and report duplicate symbol/timestamp keys."""
    failures = {
        index: validate_candle_document(document)
        for index, document in enumerate(documents)
    }
    seen: dict[tuple[str, Any], int] = {}
    for index, document in enumerate(documents):
        key = (str(document.get("symbol")), document.get("timestamp"))
        if key in seen:
            failures.setdefault(index, []).append("duplicate_timestamp")
            CANDLE_SANITY_VIOLATIONS.labels(
                symbol=str(document.get("symbol") or "unknown"),
                timeframe=str(document.get("interval") or "unknown"),
                reason="duplicate_timestamp",
            ).inc()
        else:
            seen[key] = index
    return {index: reasons for index, reasons in failures.items() if reasons}
