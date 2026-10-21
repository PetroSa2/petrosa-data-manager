from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import data_manager.api.app as api_module
from data_manager.api.routes import strategy_performance
from data_manager.services.historic_analytics import (
    _as_datetime,
    get_mysql_adapter,
    read_execution_events,
    read_table,
)


class MySQLFake:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def query_range(self, table, start, end, **kwargs):
        self.calls.append((table, start, end, kwargs))
        return self.rows

    def find_paginated(self, table, **kwargs):
        self.calls.append((table, kwargs))
        return self.rows, len(self.rows)


@pytest.mark.asyncio
async def test_execution_event_reader_filters_fills_and_reports_mysql_watermark():
    start = datetime(2026, 10, 1, tzinfo=UTC)
    end = start + timedelta(hours=1)
    rows = [
        {"event_type": "filled", "timestamp": start},
        {"event_type": "close", "timestamp": end - timedelta(minutes=1)},
        {"event_type": "partial_fill", "timestamp": end - timedelta(minutes=2)},
    ]
    mysql = MySQLFake(rows)

    selected, as_of = read_execution_events(mysql, start=start, end=end)

    assert [row["event_type"] for row in selected] == ["filled", "partial_fill"]
    assert as_of == (end - timedelta(minutes=2)).isoformat()
    assert mysql.calls[0][0] == "execution_events"


def test_historic_read_helpers_normalize_inputs_and_use_bounded_pages():
    naive = datetime(2026, 10, 1)
    assert _as_datetime(naive).tzinfo is UTC
    assert _as_datetime("2026-10-01T00:00:00Z").tzinfo is UTC
    assert _as_datetime("not-a-date") is None
    assert _as_datetime(3) is None
    assert get_mysql_adapter(None) is None
    assert get_mysql_adapter(SimpleNamespace(mysql_adapter=object())) is None

    mysql = MySQLFake([{"event_type": "filled", "timestamp": naive}])
    rows, _ = read_execution_events(mysql, strategy_id="missing")
    assert rows == []
    assert read_table(mysql, "positions") == mysql.rows


def test_historic_event_reader_rejects_rows_outside_window_and_strategy():
    start = datetime(2026, 10, 1, tzinfo=UTC)
    end = start + timedelta(hours=1)
    mysql = MySQLFake(
        [
            {"event_type": "filled", "strategy_id": "s1", "timestamp": start},
            {"event_type": "filled", "strategy_id": "s2", "timestamp": start},
            {"event_type": "filled", "strategy_id": "s1", "timestamp": end},
            {"event_type": "filled", "strategy_id": "s1", "timestamp": None},
        ]
    )

    rows, _ = read_execution_events(mysql, start=start, end=end, strategy_id="s1")

    assert len(rows) == 1
    assert rows[0]["strategy_id"] == "s1"


@pytest.mark.asyncio
async def test_strategy_performance_uses_mysql_without_mongodb():
    start = datetime(2026, 10, 1, tzinfo=UTC)
    end = start + timedelta(hours=1)
    mysql = MySQLFake(
        [
            {
                "event_type": "filled",
                "strategy_id": "s1",
                "timestamp": start + timedelta(minutes=1),
                "symbol": "BTCUSDT",
            }
        ]
    )
    api_module.db_manager = SimpleNamespace(mysql_adapter=mysql, mongodb_adapter=None)
    try:
        result = await strategy_performance.get_strategy_performance(start, end)
    finally:
        api_module.db_manager = None

    assert result["source"] == "execution_events_mysql"
    assert result["as_of"] == (start + timedelta(minutes=1)).isoformat()
    assert result["strategies"][0]["strategy_id"] == "s1"
