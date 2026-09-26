"""
Regression tests for petrosa-data-manager#331.

`GET /data/candles` previously used `limit`/`offset` only to widen the
requested time window: the full range was fetched from the database,
reversed in Python for `sort_order="desc"`, and sliced with
`candles[offset:offset+limit]`. Neither adapter applied a database-level
`LIMIT`/`OFFSET`, so raising a consumer's `limit` multiplied the rows
fetched, reversed, and string-formatted per request.

These tests cover:
  1. `GET /data/candles` calls the DB adapter's `query_range` with the
     route's `limit`/`offset`/sort direction — not a post-hoc Python slice
     of the full range.
  2. `sort_order="desc"` maps to `descending=True` so the DB-side `ORDER BY`
     (applied *before* `LIMIT`) returns the newest rows, not the oldest N
     rows of an ascending scan.
  3. The default (`limit=100`, ascending) path is unchanged for existing
     callers.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import data_manager.api.app as api_module


@pytest.fixture
def client(mock_db_manager):
    app = api_module.create_app()
    api_module.db_manager = mock_db_manager
    yield TestClient(app)
    api_module.db_manager = None


def _candle_row(hour: int) -> dict:
    return {
        "timestamp": datetime(2026, 1, 1, hour, tzinfo=UTC),
        "open_price": "1",
        "high_price": "2",
        "low_price": "0.5",
        "close_price": "1.5",
        "volume": "10",
        "quote_asset_volume": "20",
        "number_of_trades": 1,
        "symbol": "BTCUSDT",
        "interval": "1h",
    }


class TestCandleLimitPushdown:
    def test_limit_and_offset_are_pushed_to_the_db_query(self, client, mock_db_manager):
        mock_db_manager.mongodb_adapter.query_range = AsyncMock(
            return_value=[_candle_row(0)]
        )

        response = client.get("/data/candles?pair=BTCUSDT&period=1h&limit=5&offset=3")

        assert response.status_code == 200
        mock_db_manager.mongodb_adapter.query_range.assert_called_once()
        _, kwargs = mock_db_manager.mongodb_adapter.query_range.call_args
        assert kwargs["limit"] == 5
        assert kwargs["offset"] == 3
        assert kwargs["descending"] is False

    def test_sort_order_desc_maps_to_descending_true(self, client, mock_db_manager):
        mock_db_manager.mongodb_adapter.query_range = AsyncMock(
            return_value=[_candle_row(0)]
        )

        response = client.get("/data/candles?pair=BTCUSDT&period=1h&sort_order=desc")

        assert response.status_code == 200
        _, kwargs = mock_db_manager.mongodb_adapter.query_range.call_args
        assert kwargs["descending"] is True

    def test_response_data_is_not_reversed_or_resliced_in_python(
        self, client, mock_db_manager
    ):
        # The adapter is now the sole source of ordering/pagination — the
        # route must pass the DB result straight through, not
        # `list(reversed(...))` + `[offset:offset+limit]`.
        rows = [_candle_row(2), _candle_row(1), _candle_row(0)]
        mock_db_manager.mongodb_adapter.query_range = AsyncMock(return_value=rows)

        response = client.get(
            "/data/candles?pair=BTCUSDT&period=1h&sort_order=desc&limit=3"
        )

        assert response.status_code == 200
        values = response.json()["data"]
        assert len(values) == 3
        assert values[0]["timestamp"].startswith("2026-01-01T02")

    def test_default_limit_100_ascending_is_unchanged(self, client, mock_db_manager):
        mock_db_manager.mongodb_adapter.query_range = AsyncMock(return_value=[])

        response = client.get("/data/candles?pair=BTCUSDT&period=1h")

        assert response.status_code == 200
        _, kwargs = mock_db_manager.mongodb_adapter.query_range.call_args
        assert kwargs["limit"] == 100
        assert kwargs["offset"] == 0
        assert kwargs["descending"] is False
