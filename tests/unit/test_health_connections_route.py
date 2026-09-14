"""Tests for GET /health/connections — petrosa-data-manager#299.

Prior to #299 this route hardcoded pool_size=5/max_overflow=10/pool_recycle=1800
regardless of the actual MySQLAdapter configuration, so the reported pool
config could silently drift from reality (as it did: the adapter was
right-sized for #299 while this endpoint kept reporting the old numbers).
These tests assert the route now mirrors the live adapter engine_options.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import data_manager.api.app as api_module
from data_manager.api.app import create_app
from data_manager.db.mysql_adapter import MySQLAdapter


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
def test_connections_reports_live_mysql_adapter_pool_config(client):
    """The endpoint must mirror the adapter's real engine_options, not a
    hardcoded snapshot (#299 regression guard)."""
    fake_manager = MagicMock()
    fake_manager.mysql_adapter = MySQLAdapter(
        connection_string="mysql+pymysql://user:pass@host/db"
    )
    fake_manager.get_connection_stats.return_value = {
        "overall": {"initialized": True},
        "databases": {},
    }
    api_module.db_manager = fake_manager

    resp = client.get("/health/connections")

    assert resp.status_code == 200
    mysql_pool = resp.json()["pool_configuration"]["mysql"]
    assert mysql_pool == {
        "pool_size": 5,
        "max_overflow": 7,
        "pool_timeout": 30,
        "pool_recycle": 10,
    }
    # Ecosystem budget (#299 AC2): (pool_size + max_overflow) * maxReplicas(2) <= ~24
    assert (mysql_pool["pool_size"] + mysql_pool["max_overflow"]) * 2 <= 24
    # Hardening (#299 AC1): pool_recycle below the shared server wait_timeout=15s
    assert mysql_pool["pool_recycle"] < 15


@pytest.mark.unit
def test_connections_falls_back_to_defaults_when_no_adapter(client):
    """No db_manager / mysql_adapter yet (startup race) must not 500 —
    fall back to the documented hardened defaults."""
    api_module.db_manager = None

    resp = client.get("/health/connections")

    assert resp.status_code == 200
    assert resp.json()["error"] == "Database manager not available"
