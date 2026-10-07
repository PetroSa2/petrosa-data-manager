"""Public tests for the MySQL-only historic candle endpoint."""

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import data_manager.api.app as api_module


@pytest.fixture
def client(mock_db_manager):
    app = api_module.create_app()
    api_module.db_manager = mock_db_manager
    yield TestClient(app)
    api_module.db_manager = None


def _row(hour: int) -> dict:
    return {
        "timestamp": datetime(2020, 1, 1, hour, tzinfo=UTC),
        "open_price": "1",
        "high_price": "2",
        "low_price": "0.5",
        "close_price": "1.5",
        "volume": "10",
        "symbol": "BTCUSDT",
        "interval": "1h",
    }


def test_historic_route_reads_mysql_and_reports_source(client, mock_db_manager):
    mock_db_manager.mysql_adapter.query_range.return_value = [_row(0)]
    mock_db_manager.mysql_adapter.get_record_count.return_value = 1

    response = client.get(
        "/data/candles/historic?pair=BTCUSDT&period=1h&"
        "start=2020-01-01T00:00:00Z&end=2020-01-01T02:00:00Z"
    )

    assert response.status_code == 200
    assert response.json()["metadata"]["source"] == "mysql"
    assert response.json()["data"][0]["close"] == "1.5"
    mock_db_manager.mysql_adapter.query_range.assert_called_once()
    mock_db_manager.mongodb_adapter.query_range.assert_not_called()


def test_historic_route_returns_empty_without_mongo_fallback(client, mock_db_manager):
    response = client.get(
        "/data/candles/historic?pair=BTCUSDT&period=1h&"
        "start=2020-01-01T00:00:00Z&end=2020-01-01T02:00:00Z"
    )

    assert response.status_code == 200
    assert response.json()["data"] == []
    assert response.json()["pagination"]["total"] == 0
    mock_db_manager.mongodb_adapter.query_range.assert_not_called()


def test_realtime_route_never_reads_mysql(client, mock_db_manager):
    with patch("data_manager.api.routes.data.constants.CANDLE_DATABASE_TYPE", "mysql"):
        response = client.get("/data/candles?pair=BTCUSDT&period=1h")

    assert response.status_code == 200
    mock_db_manager.mysql_adapter.query_range.assert_not_called()
    mock_db_manager.mysql_adapter.get_record_count.assert_not_called()


def test_historic_route_rejects_unsupported_period_without_database_calls(
    client, mock_db_manager
):
    response = client.get(
        "/data/candles/historic?pair=BTCUSDT&period=2h&"
        "start=2020-01-01T00:00:00Z&end=2020-01-01T02:00:00Z"
    )

    assert response.status_code == 422
    mock_db_manager.mysql_adapter.query_range.assert_not_called()
    mock_db_manager.mongodb_adapter.query_range.assert_not_called()
