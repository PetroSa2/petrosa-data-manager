"""Tests for the historical strategy performance endpoint."""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

import data_manager.api.app as api_module
from data_manager.api.app import create_app
from data_manager.services.strategy_performance import build_strategy_performance


def test_strategy_performance_aggregates_close_events():
    start = datetime(2026, 10, 1, tzinfo=UTC)
    end = datetime(2026, 10, 2, tzinfo=UTC)
    rows: list[dict[str, Any]] = [
        {
            "event_type": "position_closed",
            "strategy_id": "S1",
            "pnl": 12.5,
            "net_r": 1.25,
        },
        {
            "event_type": "unattributed_exchange_close",
            "strategy_id": "S1",
            "pnl": -2.5,
            "net_r": -0.25,
        },
    ]
    cursor = MagicMock()
    cursor.sort.return_value = cursor
    cursor.to_list = AsyncMock(return_value=rows)
    collection = MagicMock()
    collection.find.return_value = cursor
    mongodb = MagicMock()
    mongodb.db = {"execution_events": collection}
    api_module.db_manager = MagicMock(mongodb_adapter=mongodb)
    try:
        client = TestClient(create_app())
        response = client.get(
            "/api/v1/strategy/performance",
            params={"from": start.isoformat(), "to": end.isoformat()},
        )
        assert response.status_code == 200
        row = response.json()["strategies"][0]
        assert row["strategy_id"] == "S1"
        assert row["realized_pnl_usd"] == 10.0
        assert row["win_rate"] == 0.5
        assert row["trade_count"] == 2
        assert row["net_r_total"] == 1.0
        assert response.json()["source"] == "execution_events"
    finally:
        api_module.db_manager = None


def test_strategy_performance_replays_fill_only_history():
    rows = [
        {
            "event_type": "filled",
            "strategy_id": "S1",
            "symbol": "BTCUSDT",
            "side": "buy",
            "fill_qty": 1,
            "price": 100,
            "timestamp": datetime(2026, 10, 1, tzinfo=UTC),
        },
        {
            "event_type": "partial_fill",
            "strategy_id": "S1",
            "symbol": "BTCUSDT",
            "side": "sell",
            "fill_qty": 1,
            "price": 110,
            "timestamp": datetime(2026, 10, 1, 1, tzinfo=UTC),
        },
    ]
    report = build_strategy_performance(
        rows,
        start=datetime(2026, 10, 1, tzinfo=UTC),
        end=datetime(2026, 10, 2, tzinfo=UTC),
    )
    assert report[0]["realized_pnl_usd"] == 10
    assert report[0]["trade_count"] == 1
    assert report[0]["win_rate"] == 1


def test_strategy_performance_rejects_invalid_window():
    api_module.db_manager = MagicMock()
    try:
        client = TestClient(create_app())
        response = client.get(
            "/api/v1/strategy/performance",
            params={"from": "2026-10-02T00:00:00Z", "to": "2026-10-01T00:00:00Z"},
        )
        assert response.status_code == 422
    finally:
        api_module.db_manager = None
