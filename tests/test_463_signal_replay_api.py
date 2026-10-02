"""Contract tests for the point-in-time signal API."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import data_manager.api.app as api_module
import data_manager.api.routes.signals as signals_route


@pytest.fixture
def signal_client(mock_db_manager, monkeypatch):
    class FakeRepository:
        def upsert(self, signal):
            return {"status": "inserted", "signal_key": signal.signal_key}

        def replay(self, **kwargs):
            assert kwargs["include_legacy"] is False
            return (
                [
                    {
                        "signal_key": "s-1",
                        "bar_open_time": datetime(2026, 1, 1, tzinfo=UTC),
                    }
                ],
                1,
            )

        def coverage(self, **kwargs):
            return {
                "signal_count": 2,
                "klines_present": 1,
                "missing_kline_bars": [datetime(2026, 1, 1, tzinfo=UTC)],
                "kline_source": "mysql",
                "last_kline_time": datetime(2026, 1, 1, tzinfo=UTC),
                "legacy_rows": 0,
            }

    monkeypatch.setattr(signals_route, "_repository", lambda: FakeRepository())
    api_module.db_manager = SimpleNamespace(mysql_adapter=object())
    app = api_module.create_app()
    with TestClient(app) as client:
        yield client
    api_module.db_manager = mock_db_manager


def test_signal_upsert_route(signal_client):
    response = signal_client.post(
        "/api/v1/signals",
        json={
            "symbol": "BTCUSDT",
            "signal_key": "s-1",
            "bar_open_time": "2026-01-01T00:00:00Z",
        },
    )
    assert response.status_code == 200
    assert response.json()["signal_key"] == "s-1"


def test_replay_is_paginated_and_excludes_legacy_by_default(signal_client):
    response = signal_client.get("/api/v1/signals/replay?limit=1&offset=0")
    assert response.status_code == 200
    assert response.json()["pagination"] == {
        "total": 1,
        "limit": 1,
        "offset": 0,
        "has_next": False,
    }


def test_coverage_reports_missing_bars(signal_client):
    response = signal_client.get("/api/v1/signals/replay/coverage")
    assert response.status_code == 200
    assert response.json()["klines_present"] == 1
    assert response.json()["kline_source"] == "mysql"


def test_replay_rejects_reverse_time_range(signal_client):
    response = signal_client.get(
        "/api/v1/signals/replay?from=2026-01-02T00:00:00Z&to=2026-01-01T00:00:00Z"
    )
    assert response.status_code == 400
