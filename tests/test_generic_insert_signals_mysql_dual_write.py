"""
Regression tests for POST /api/v1/{database}/{collection} (generic.py::insert_records)
— the MySQL `signals` dual-write added 2026-09-20.

MySQL `petrosa_crypto.signals` is the durable historic store going forward
(its own legacy writer was removed by petrosa-bot-ta-analysis#284, leaving a
frozen 14.38M-row archive); Mongo `signals` was cut to a 1-hour rolling
window (`intents_ttl_index.DEFAULT_SIGNALS_TTL_SECONDS`) since it can no
longer double as long-term storage. Every `mongodb.signals` insert now also
best-effort dual-writes a mapped record into MySQL `signals`, gated
independently by `PETROSA_SIGNALS_MYSQL_PERSIST_ENABLED`.
"""

from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

import data_manager.api.app as api_module
from data_manager.api.routes.generic import (
    _build_mysql_signal_record,
    _signals_mysql_persist_enabled,
)
from data_manager.db.mysql_adapter import WriteResult


@pytest.fixture
def client(mock_db_manager):
    """Create test client with mocked Mongo + MySQL adapters."""
    mock_db_manager.mongodb_adapter = Mock()
    mock_db_manager.mongodb_adapter.write = AsyncMock(return_value=1)

    mock_db_manager.mysql_adapter = Mock()
    mock_db_manager.mysql_adapter.write = Mock(
        return_value=WriteResult(inserted=1, duplicates=0, failed=0)
    )

    app = api_module.create_app()
    api_module.db_manager = mock_db_manager
    yield TestClient(app)
    api_module.db_manager = None


class TestKillSwitch:
    def test_defaults_to_true(self, monkeypatch):
        monkeypatch.delenv("PETROSA_SIGNALS_MYSQL_PERSIST_ENABLED", raising=False)
        assert _signals_mysql_persist_enabled() is True

    def test_false_when_env_false(self, monkeypatch):
        monkeypatch.setenv("PETROSA_SIGNALS_MYSQL_PERSIST_ENABLED", "false")
        assert _signals_mysql_persist_enabled() is False


class TestBuildMysqlSignalRecord:
    def test_maps_action_to_signal_type_and_timeframe_to_period(self):
        record = _build_mysql_signal_record(
            {
                "symbol": "BTCUSDT",
                "action": "buy",
                "confidence": 0.85,
                "strategy": "ema_pullback_continuation",
                "timeframe": "5m",
                "metadata": {"ema9": 1.0, "stop_loss": 2.0},
                "timestamp": "2026-09-20T00:00:00Z",
            }
        )
        assert record == {
            "symbol": "BTCUSDT",
            "timeframe": "5m",
            "period": "5m",  # legacy convention: period mirrors timeframe
            "signal_type": "buy",
            "confidence": 0.85,
            "strategy": "ema_pullback_continuation",
            "metadata": {"ema9": 1.0, "stop_loss": 2.0},
            "timestamp": "2026-09-20T00:00:00Z",
        }

    def test_falls_back_to_strategy_id_when_strategy_missing(self):
        record = _build_mysql_signal_record(
            {"symbol": "ETHUSDT", "action": "sell", "strategy_id": "strat-42"}
        )
        assert record["strategy"] == "strat-42"

    def test_defaults_timeframe_and_signal_type_when_absent(self):
        record = _build_mysql_signal_record({"symbol": "ETHUSDT"})
        assert record["timeframe"] == "15m"
        assert record["period"] == "15m"
        assert record["signal_type"] == "hold"
        assert record["confidence"] == 0.0
        assert record["strategy"] == ""
        assert record["metadata"] == {}

    def test_id_and_created_at_never_included(self):
        record = _build_mysql_signal_record({"symbol": "BTCUSDT", "action": "buy"})
        assert "id" not in record
        assert "created_at" not in record


class TestDualWriteIntegration:
    def test_mongo_signals_insert_dual_writes_to_mysql(self, client):
        response = client.post(
            "/api/v1/mongodb/signals",
            json={
                "data": {
                    "symbol": "BTCUSDT",
                    "action": "buy",
                    "confidence": 0.9,
                    "strategy": "ema_pullback_continuation",
                    "timeframe": "5m",
                    "metadata": {"ema9": 1.0},
                    "timestamp": "2026-09-20T00:00:00Z",
                }
            },
        )

        assert response.status_code == 200
        api_module.db_manager.mongodb_adapter.write.assert_called_once()
        api_module.db_manager.mysql_adapter.write.assert_called_once()

        (records, collection), _kwargs = (
            api_module.db_manager.mysql_adapter.write.call_args
        )
        assert collection == "signals"
        assert len(records) == 1
        dumped = records[0].model_dump()
        assert dumped["signal_type"] == "buy"
        assert dumped["period"] == "5m"

    def test_batch_insert_dual_writes_every_item(self, client):
        response = client.post(
            "/api/v1/mongodb/signals",
            json={
                "data": [
                    {"symbol": "BTCUSDT", "action": "buy"},
                    {"symbol": "ETHUSDT", "action": "sell"},
                ]
            },
        )

        assert response.status_code == 200
        (records, _collection), _kwargs = (
            api_module.db_manager.mysql_adapter.write.call_args
        )
        assert len(records) == 2

    def test_mysql_kill_switch_off_skips_mysql_but_mongo_still_writes(
        self, client, monkeypatch
    ):
        monkeypatch.setenv("PETROSA_SIGNALS_MYSQL_PERSIST_ENABLED", "false")

        response = client.post(
            "/api/v1/mongodb/signals",
            json={"data": {"symbol": "BTCUSDT", "action": "buy"}},
        )

        assert response.status_code == 200
        api_module.db_manager.mongodb_adapter.write.assert_called_once()
        api_module.db_manager.mysql_adapter.write.assert_not_called()

    def test_mongo_kill_switch_off_does_not_block_mysql_dual_write(
        self, client, monkeypatch
    ):
        """The two stores are independently toggleable — disabling the Mongo
        write must not stop the MySQL historic write, and vice versa."""
        monkeypatch.setenv("PETROSA_SIGNALS_PERSIST_ENABLED", "false")

        response = client.post(
            "/api/v1/mongodb/signals",
            json={"data": {"symbol": "BTCUSDT", "action": "buy"}},
        )

        assert response.status_code == 200
        api_module.db_manager.mongodb_adapter.write.assert_not_called()
        api_module.db_manager.mysql_adapter.write.assert_called_once()

    def test_mysql_write_failure_does_not_fail_the_request(self, client):
        api_module.db_manager.mysql_adapter.write = Mock(
            side_effect=RuntimeError("mysql down")
        )

        response = client.post(
            "/api/v1/mongodb/signals",
            json={"data": {"symbol": "BTCUSDT", "action": "buy"}},
        )

        assert response.status_code == 200
        assert response.json()["inserted_count"] == 1
        api_module.db_manager.mongodb_adapter.write.assert_called_once()

    def test_missing_mysql_adapter_does_not_raise(self, client):
        api_module.db_manager.mysql_adapter = None

        response = client.post(
            "/api/v1/mongodb/signals",
            json={"data": {"symbol": "BTCUSDT", "action": "buy"}},
        )

        assert response.status_code == 200
        assert response.json()["inserted_count"] == 1

    def test_missing_symbol_is_skipped_not_raised(self, client):
        api_module.db_manager.mysql_adapter.write = Mock(
            return_value=WriteResult(inserted=0, duplicates=0, failed=0)
        )

        response = client.post(
            "/api/v1/mongodb/signals",
            json={"data": {"action": "buy"}},
        )

        assert response.status_code == 200
        api_module.db_manager.mysql_adapter.write.assert_not_called()

    def test_non_signals_collection_does_not_trigger_dual_write(self, client):
        response = client.post(
            "/api/v1/mongodb/alerts",
            json={"data": {"symbol": "BTCUSDT", "message": "test"}},
        )

        assert response.status_code == 200
        api_module.db_manager.mysql_adapter.write.assert_not_called()

    def test_direct_mysql_signals_path_is_not_double_written(self, client):
        """Posting straight to /api/v1/mysql/signals must call mysql write
        exactly once (via the normal generic path) — not twice via the
        dual-write, which only triggers off the mongodb.signals branch."""
        response = client.post(
            "/api/v1/mysql/signals",
            json={"data": {"symbol": "BTCUSDT", "action": "buy"}},
        )

        assert response.status_code == 200
        api_module.db_manager.mysql_adapter.write.assert_called_once()

    def test_direct_mysql_insert_reports_ignored_count(self, client):
        api_module.db_manager.mysql_adapter.write = Mock(
            return_value=WriteResult(
                inserted=2, duplicates=1, failed=0, ignored_count=1
            )
        )

        response = client.post(
            "/api/v1/mysql/signals",
            json={
                "data": [
                    {"symbol": "BTCUSDT", "action": "buy"},
                    {"symbol": "ETHUSDT", "action": "sell"},
                    {"symbol": "SOLUSDT", "action": "hold"},
                ]
            },
        )

        assert response.status_code == 200
        assert response.json()["inserted_count"] == 2
        assert response.json()["ignored_count"] == 1
