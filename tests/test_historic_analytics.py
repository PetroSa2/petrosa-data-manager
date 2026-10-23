import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import data_manager.api.app as api_module
from data_manager.api.routes import analysis, strategy_performance
from data_manager.services.historic_analytics import (
    _as_datetime,
    get_mysql_adapter,
    read_execution_events,
    read_table,
)
from data_manager.services.strategy_performance import build_strategy_performance


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
    assert mysql.calls[-1][1]["include_total"] is False
    assert mysql.calls[-1][1]["filter_dict"] == {"strategy_id": "missing"}
    assert mysql.calls[-1][1]["columns"] == (
        "timestamp",
        "decision_id",
        "order_id",
        "strategy_id",
        "event_type",
        "symbol",
        "side",
        "qty",
        "fill_qty",
        "fill_quantity",
        "price",
        "fill_price",
        "fill_time",
        "reason",
        "payload",
    )
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


def test_strategy_performance_builds_explicit_zero_sample_for_active_strategy():
    start = datetime(2026, 10, 1, tzinfo=UTC)
    result = build_strategy_performance(
        [], start=start, end=start + timedelta(hours=1), strategy_ids=["active"]
    )

    assert result == [
        {
            "strategy_id": "active",
            "realized_pnl_usd": 0.0,
            "win_rate": 0.0,
            "trade_count": 0,
            "net_r_total": 0.0,
            "from": start.isoformat(),
            "to": (start + timedelta(hours=1)).isoformat(),
        }
    ]


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


@pytest.mark.asyncio
async def test_strategy_performance_empty_mysql_history_is_incomplete():
    api_module.db_manager = SimpleNamespace(
        mysql_adapter=MySQLFake([]), mongodb_adapter=None
    )
    try:
        result = await strategy_performance.get_strategy_performance(
            datetime(2026, 10, 1, tzinfo=UTC),
            datetime(2026, 10, 2, tzinfo=UTC),
        )
    finally:
        api_module.db_manager = None

    assert result["status"] == "incomplete"
    assert result["source_errors"]["execution_events"] == "no usable rows"


@pytest.mark.asyncio
async def test_legacy_strategy_performance_uses_mysql_without_mongodb():
    mysql = MySQLFake([])
    api_module.db_manager = SimpleNamespace(mysql_adapter=mysql, mongodb_adapter=None)
    try:
        result = await analysis.get_strategy_performance("active")
    finally:
        api_module.db_manager = None

    assert result["stats"]["win_rate"] == 0.0
    assert result["stats"]["consecutive_losses"] == 0
    assert result["metadata"]["history_source"] == "execution_events_mysql"
    assert result["metadata"]["history_status"] == "zero_sample"
    assert mysql.calls[0][1]["filter_dict"] == {"strategy_id": "active"}
    assert mysql.calls[0][1]["include_total"] is False


@pytest.mark.asyncio
async def test_legacy_strategy_performance_mysql_read_is_bounded(monkeypatch):
    mysql = MySQLFake([])
    api_module.db_manager = SimpleNamespace(mysql_adapter=mysql, mongodb_adapter=None)

    async def slow_to_thread(*_args, **_kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(analysis.asyncio, "to_thread", slow_to_thread)
    monkeypatch.setattr(
        analysis.constants, "ANALYTICS_DEPENDENCY_TIMEOUT_SECONDS", 0.01
    )
    try:
        with pytest.raises(analysis._AnalyticsDependencyTimeout):
            await analysis._load_execution_events_once(None, "active")
    finally:
        api_module.db_manager = None
