"""Canonical candle sets for degenerate auditor inputs."""

from datetime import UTC, datetime, timedelta

_START = datetime(2026, 1, 1, tzinfo=UTC)


def _candle(
    timestamp: datetime,
    open_price: float,
    high: float,
    low: float,
    close: float,
    volume: float,
) -> dict:
    return {
        "timestamp": timestamp,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


FLAT_BAR = [
    _candle(_START, 100.0, 100.0, 100.0, 100.0, 10.0),
]
FLAT_BAR_DEFECT = "flat OHLC values exercise zero-range candle handling"

ZERO_PRICED_BAR = [
    _candle(_START, 1.0, 2.0, 0.0, 1.0, 10.0),
]
ZERO_PRICED_BAR_DEFECT = "a zero low price exercises division and normalization guards"

ALL_ZERO_ROLLING_WINDOW = [
    _candle(_START + timedelta(minutes=5 * index), 0.0, 0.0, 0.0, 0.0, 0.0)
    for index in range(20)
]
ALL_ZERO_ROLLING_WINDOW_DEFECT = (
    "an all-zero rolling window exercises zero-variance and zero-volume handling"
)

SHORT_SERIES = [
    _candle(_START, 100.0, 101.0, 99.0, 100.5, 10.0),
    _candle(_START + timedelta(minutes=5), 100.5, 101.5, 100.0, 101.0, 11.0),
]
SHORT_SERIES_DEFECT = (
    "fewer than warmup minimum candles exercise undersized-series handling"
)

DEGENERATE_CANDLE_SETS = (
    ("flat_bar", FLAT_BAR, FLAT_BAR_DEFECT),
    ("zero_priced_bar", ZERO_PRICED_BAR, ZERO_PRICED_BAR_DEFECT),
    (
        "all_zero_rolling_window",
        ALL_ZERO_ROLLING_WINDOW,
        ALL_ZERO_ROLLING_WINDOW_DEFECT,
    ),
    ("short_series", SHORT_SERIES, SHORT_SERIES_DEFECT),
)
