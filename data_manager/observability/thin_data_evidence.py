"""Prometheus evidence for thin-data-related consumer failures."""

from prometheus_client import Counter

THIN_DATA_CONSUMER_ERRORS = Counter(
    "data_manager_thin_data_consumer_errors_total",
    "Thin-data-related consumer failures observed by data-manager",
    ["consumer", "error_type", "symbol", "timeframe"],
)


def record_thin_data_consumer_error(
    *,
    consumer: str,
    error_type: str,
    symbol: str = "",
    timeframe: str = "",
) -> None:
    """Record a bounded, queryable consumer error without retaining payload data."""
    THIN_DATA_CONSUMER_ERRORS.labels(
        consumer=consumer,
        error_type=error_type,
        symbol=symbol,
        timeframe=timeframe,
    ).inc()
