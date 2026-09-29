"""
Regression tests for the retirement of the never-populated klines timeframes.

`GET /data/candles` accepted any `period` string. That value flows into a
collection/table name (`mysql_table_name` / `mongo_collection_name`), so
`?period=1m` or `?period=4h` resolved to `klines_m1` / `klines_h4` — MySQL
tables created by a one-off 2026-03-13 schema DDL that no extractor CronJob has
ever written to. They held 0 rows for the life of the system.

Migration 011 drops those seven tables and their seven compatibility views, so
leaving the endpoint permissive would convert a silent empty result into a
runtime "table doesn't exist" error. These tests cover:

  1. `constants.SUPPORTED_TIMEFRAMES` no longer advertises 1m/4h and contains
     exactly the timeframes the extractor and gap-filler are configured to fill.
  2. The candles endpoint rejects a retired timeframe with 422 *before*
     touching the database — no query is issued, so no missing table is reached.
  3. Every supported timeframe still reaches the adapter (no over-blocking).
  4. The rejection names the supported set, so the failure is self-documenting.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import constants
import data_manager.api.app as api_module

#: The timeframes petrosa_k8s actually creates CronJobs for. Kept literal on
#: purpose: this is the contract between the k8s manifests and this repo, so a
#: drift here should fail the suite rather than be silently absorbed.
EXTRACTOR_TIMEFRAMES = {"5m", "15m", "30m", "1h", "1d"}

#: Timeframes whose MySQL tables migration 011 retires.
RETIRED_TIMEFRAMES = {"1m", "3m", "2h", "4h", "6h", "8h", "12h"}


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


class TestSupportedTimeframes:
    def test_retired_timeframes_are_not_advertised(self):
        assert not (set(constants.SUPPORTED_TIMEFRAMES) & RETIRED_TIMEFRAMES), (
            f"migration 011 drops the backing tables for "
            f"{sorted(set(constants.SUPPORTED_TIMEFRAMES) & RETIRED_TIMEFRAMES)}"
        )

    def test_exactly_the_extractor_timeframes_are_supported(self):
        assert set(constants.SUPPORTED_TIMEFRAMES) == EXTRACTOR_TIMEFRAMES

    def test_execution_intervals_are_a_subset(self):
        """The execution path must never request a timeframe the writer skips."""
        assert set(constants.SUPPORTED_INTERVALS) <= set(
            constants.SUPPORTED_TIMEFRAMES
        ), (
            f"execution path requests {sorted(set(constants.SUPPORTED_INTERVALS) - set(constants.SUPPORTED_TIMEFRAMES))}, "
            "which no extractor populates"
        )

    def test_no_duplicates(self):
        assert len(constants.SUPPORTED_TIMEFRAMES) == len(
            set(constants.SUPPORTED_TIMEFRAMES)
        )


class TestCandlesEndpointRejectsRetiredTimeframes:
    @pytest.mark.parametrize("period", sorted(RETIRED_TIMEFRAMES))
    def test_retired_period_is_rejected(self, client, mock_db_manager, period):
        mock_db_manager.mongodb_adapter.query_range = AsyncMock(return_value=[])

        response = client.get(f"/data/candles?pair=BTCUSDT&period={period}")

        assert response.status_code == 422, response.text
        assert mock_db_manager.mongodb_adapter.query_range.await_count == 0, (
            f"period={period} reached the database adapter; the rejection must "
            "happen before a collection/table name is built"
        )

    def test_rejection_lists_the_supported_set(self, client, mock_db_manager):
        mock_db_manager.mongodb_adapter.query_range = AsyncMock(return_value=[])

        response = client.get("/data/candles?pair=BTCUSDT&period=1m")

        assert response.status_code == 422
        detail = response.json()["detail"]
        for period in EXTRACTOR_TIMEFRAMES:
            assert period in detail
        assert "1m" not in detail.replace("Unsupported period '1m'", "")

    def test_1m_and_4h_are_rejected_even_though_they_were_formerly_listed(
        self, client, mock_db_manager
    ):
        """These two are the ones the public API's own help text used to
        advertise as examples, so they are the most likely caller inputs."""
        mock_db_manager.mongodb_adapter.query_range = AsyncMock(return_value=[])

        for period in ("1m", "4h"):
            response = client.get(f"/data/candles?pair=BTCUSDT&period={period}")
            assert response.status_code == 422, f"{period}: {response.text}"


class TestSupportedTimeframesStillWork:
    @pytest.mark.parametrize("period", sorted(EXTRACTOR_TIMEFRAMES))
    def test_supported_period_reaches_the_adapter(
        self, client, mock_db_manager, period
    ):
        mock_db_manager.mongodb_adapter.query_range = AsyncMock(
            return_value=[_candle_row(0)]
        )

        response = client.get(f"/data/candles?pair=BTCUSDT&period={period}")

        assert response.status_code == 200, response.text
        assert mock_db_manager.mongodb_adapter.query_range.await_count == 1

    def test_period_case_and_whitespace_are_normalized(self, client, mock_db_manager):
        """`?period= 5M ` was previously forwarded verbatim into a collection
        name. Normalizing here keeps the validation from being bypassed by
        casing while still accepting sloppy-but-harmless input."""
        mock_db_manager.mongodb_adapter.query_range = AsyncMock(
            return_value=[_candle_row(0)]
        )

        response = client.get("/data/candles?pair=BTCUSDT&period=%205M%20")

        assert response.status_code == 200, response.text
        assert response.json()["parameters"]["period"] == "5m"

    def test_malformed_period_is_rejected_not_500(self, client, mock_db_manager):
        mock_db_manager.mongodb_adapter.query_range = AsyncMock(return_value=[])

        response = client.get("/data/candles?pair=BTCUSDT&period=not-a-timeframe")

        assert response.status_code == 422
