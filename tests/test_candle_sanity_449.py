from datetime import UTC, datetime, timedelta
from decimal import Decimal

from data_manager.maintenance.candle_sanity import (
    validate_candle_batch,
    validate_candle_document,
)


def candle(**overrides):
    value = {
        "symbol": "BTCUSDT",
        "interval": "5m",
        "timestamp": datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        "open_price": "10",
        "high_price": "12",
        "low_price": "9",
        "close_price": "11",
        "volume": "1",
    }
    value.update(overrides)
    return value


def test_sanity_sentinels_reject_bad_ohlcv_and_future_data():
    reasons = validate_candle_document(
        candle(
            low_price="13",
            volume="-1",
            timestamp=datetime.now(UTC) + timedelta(minutes=5),
        )
    )

    assert {"low_above_price", "negative_volume", "future_timestamp"} <= set(reasons)


def test_sanity_sentinels_reject_unaligned_and_duplicate_candles():
    first = candle(timestamp=datetime(2026, 1, 1, 0, 1, tzinfo=UTC))
    duplicate = candle(timestamp=first["timestamp"])

    failures = validate_candle_batch([first, duplicate])

    assert "unaligned_timestamp" in failures[0]
    assert "duplicate_timestamp" in failures[1]


def test_valid_candle_has_no_sanity_failures():
    assert validate_candle_document(candle()) == []
    assert Decimal("1") >= 0
