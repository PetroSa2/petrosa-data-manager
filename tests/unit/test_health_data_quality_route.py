"""Tests for GET /health (data-quality view) — closes #281.

Prior to this fix the route always returned hardcoded fake values
(completeness=99.9, gaps=0, duplicates=0) for any pair/period, even though
`HealthRepository.get_latest_health` was real and DB-backed but never
called. These tests assert the route now calls the real repository.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import data_manager.api.app as api_module
from data_manager.api.app import create_app

_HEALTH_ROW = {
    "metric_id": "m-1",
    "dataset_id": "ds-1",
    "symbol": "BTCUSDT",
    "completeness": 87.5,
    "freshness_seconds": 120,
    "gaps_count": 3,
    "duplicates_count": 1,
    "quality_score": 80.0,
    "timestamp": datetime(2026, 3, 1, tzinfo=UTC),
}


@pytest.fixture
def client():
    app = create_app()
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_db_manager():
    original = api_module.db_manager
    yield
    api_module.db_manager = original


@pytest.mark.unit
def test_data_health_returns_real_metrics_when_available(client):
    fake_manager = MagicMock()
    fake_manager.mysql_adapter = MagicMock()
    fake_manager.mongodb_adapter = MagicMock()
    fake_manager.mysql_adapter.query_latest.return_value = [_HEALTH_ROW]
    api_module.db_manager = fake_manager

    resp = client.get("/health", params={"pair": "BTCUSDT"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["health"]["completeness"] == 87.5
    assert body["health"]["gaps"] == 3
    assert body["health"]["duplicates"] == 1
    assert body["health"]["quality_score"] == 80.0
    # No longer the old hardcoded 99.9/0/0 stub for any pair.
    assert body["health"] != {
        "completeness": 99.9,
        "freshness_sec": 5,
        "gaps": 0,
        "duplicates": 0,
        "consistency_score": 100.0,
        "quality_score": 99.5,
    }
    assert body["metadata"].get("no_data") is not True


@pytest.mark.unit
def test_data_health_returns_no_data_flag_when_no_metrics_row(client):
    fake_manager = MagicMock()
    fake_manager.mysql_adapter = MagicMock()
    fake_manager.mongodb_adapter = MagicMock()
    fake_manager.mysql_adapter.query_latest.return_value = []
    api_module.db_manager = fake_manager

    resp = client.get("/health", params={"pair": "UNKNOWNPAIR"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["metadata"]["no_data"] is True
    assert body["health"]["completeness"] == 0.0


@pytest.mark.unit
def test_data_health_graceful_when_db_unavailable(client):
    api_module.db_manager = None

    resp = client.get("/health", params={"pair": "BTCUSDT"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["metadata"]["no_data"] is True
