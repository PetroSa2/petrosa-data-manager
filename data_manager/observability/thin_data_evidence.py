"""Prometheus evidence for thin-data-related consumer failures."""

from prometheus_client import Counter

THIN_DATA_CONSUMER_ERRORS = Counter(
    "data_manager_thin_data_consumer_errors_total",
    "Thin-data-related consumer failures observed by data-manager",
    ["consumer", "error_type", "symbol", "timeframe"],
)

EXECUTION_EVENT_VALIDATION_ERRORS = Counter(
    "data_manager_execution_event_validation_errors_total",
    "Execution-event validation failures grouped by producer, subject, and field",
    ["producer", "subject", "validation_field"],
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


def record_execution_event_validation_error(
    *, producer: str, subject: str, validation_field: str
) -> None:
    """Record bounded execution-event validation evidence without payload data."""
    EXECUTION_EVENT_VALIDATION_ERRORS.labels(
        producer=producer,
        subject=subject,
        validation_field=validation_field,
    ).inc()
