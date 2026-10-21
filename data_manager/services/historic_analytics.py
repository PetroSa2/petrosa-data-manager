"""Bounded readers for analytics data in the durable MySQL copy."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from data_manager.services.round_book import FILL_EVENT_TYPES

MAX_ANALYTICS_ROWS = 200_000


def get_mysql_adapter(manager: Any) -> Any:
    adapter = getattr(manager, "__dict__", {}).get("mysql_adapter") if manager else None
    if adapter is None:
        return None
    if not callable(getattr(adapter, "query_range", None)):
        return None
    return adapter


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _watermark(rows: list[dict[str, Any]]) -> str | None:
    dates = [parsed for row in rows if (parsed := _as_datetime(row.get("timestamp")))]
    return max(dates).isoformat() if dates else None


def _filter_events(
    rows: list[dict[str, Any]],
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    strategy_id: str | None = None,
    event_types: set[str] = FILL_EVENT_TYPES,
) -> list[dict[str, Any]]:
    selected = []
    for row in rows:
        if row.get("event_type") not in event_types:
            continue
        if strategy_id is not None and str(row.get("strategy_id") or "") != strategy_id:
            continue
        timestamp = _as_datetime(row.get("timestamp"))
        if start and (timestamp is None or timestamp < start):
            continue
        if end and (timestamp is None or timestamp >= end):
            continue
        selected.append(row)
    return selected


def read_execution_events(
    mysql: Any,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    strategy_id: str | None = None,
    limit: int = MAX_ANALYTICS_ROWS,
    event_types: set[str] = FILL_EVENT_TYPES,
) -> tuple[list[dict[str, Any]], str | None]:
    if start is not None and end is not None:
        rows = mysql.query_range("execution_events", start, end, limit=limit)
    else:
        rows, _ = mysql.find_paginated(
            "execution_events", sort_list=[("timestamp", 1)], limit=limit
        )
    selected = _filter_events(
        rows,
        start=start,
        end=end,
        strategy_id=strategy_id,
        event_types=event_types,
    )
    return selected, _watermark(selected)


def read_table(
    mysql: Any, table: str, *, limit: int = MAX_ANALYTICS_ROWS
) -> list[dict[str, Any]]:
    rows, _ = mysql.find_paginated(table, sort_list=[("timestamp", 1)], limit=limit)
    return rows
