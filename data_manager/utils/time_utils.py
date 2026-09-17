"""
Time utility functions for Data Manager.
"""

from datetime import datetime, timedelta, timezone

try:
    from datetime import UTC
except ImportError:  # pragma: no cover - py<3.11 fallback, matches rest of repo
    UTC = timezone.utc  # noqa: UP017


def as_aware_utc(value: datetime | str) -> datetime:
    """Normalize a timestamp value to a timezone-aware UTC ``datetime``.

    MongoDB drivers (PyMongo/Motor) return **naive** ``datetime`` objects for
    BSON dates by default -- the driver does not attach ``tzinfo`` even
    though the underlying value is always UTC. Mixing such a naive value
    with an aware ``datetime.now(timezone.utc)`` in a subtraction or
    comparison raises ``TypeError: can't subtract/compare offset-naive and
    offset-aware datetimes`` (petrosa-data-manager#312).

    Mirrors the ``_as_aware_utc()`` helper pattern already used in
    ``petrosa-otel`` ``rate_limiter.py`` (petrosa_k8s#1095 / commit
    ``992edc8b``).

    Args:
        value: An ISO-format string or a ``datetime`` (naive or aware).

    Returns:
        An aware UTC ``datetime``. Naive values are treated as already UTC,
        matching how MongoDB stores BSON dates internally.
    """
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value


def parse_timeframe_to_minutes(timeframe: str) -> int:
    """
    Convert timeframe string to minutes.

    Args:
        timeframe: Timeframe string (e.g., '1m', '5m', '1h', '1d')

    Returns:
        Number of minutes

    Examples:
        '1m' -> 1
        '5m' -> 5
        '15m' -> 15
        '1h' -> 60
        '4h' -> 240
        '1d' -> 1440
    """
    timeframe = timeframe.lower()

    if timeframe.endswith("m"):
        return int(timeframe[:-1])
    elif timeframe.endswith("h"):
        return int(timeframe[:-1]) * 60
    elif timeframe.endswith("d"):
        return int(timeframe[:-1]) * 1440
    elif timeframe.endswith("w"):
        return int(timeframe[:-1]) * 10080
    else:
        raise ValueError(f"Invalid timeframe: {timeframe}")


def parse_timeframe_to_seconds(timeframe: str) -> int:
    """
    Convert timeframe string to seconds.

    Args:
        timeframe: Timeframe string (e.g., '1m', '5m', '1h', '1d')

    Returns:
        Number of seconds
    """
    return parse_timeframe_to_minutes(timeframe) * 60


def calculate_expected_records(start: datetime, end: datetime, timeframe: str) -> int:
    """
    Calculate expected number of records in a time range.

    Args:
        start: Start datetime
        end: End datetime
        timeframe: Timeframe string

    Returns:
        Expected number of records
    """
    interval_seconds = parse_timeframe_to_seconds(timeframe)
    total_seconds = (end - start).total_seconds()
    return int(total_seconds / interval_seconds)


def create_time_chunks(
    start: datetime, end: datetime, chunk_size_minutes: int = 60
) -> list[tuple[datetime, datetime]]:
    """
    Split time range into chunks.

    Args:
        start: Start datetime
        end: End datetime
        chunk_size_minutes: Size of each chunk in minutes

    Returns:
        List of (chunk_start, chunk_end) tuples
    """
    chunks = []
    current = start
    chunk_delta = timedelta(minutes=chunk_size_minutes)

    while current < end:
        chunk_end = min(current + chunk_delta, end)
        chunks.append((current, chunk_end))
        current = chunk_end

    return chunks
