"""Generic MySQL endpoints are historic reads, not operational write paths."""

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import constants
import data_manager.api.app as api_module


@pytest.fixture
def client(mock_db_manager, monkeypatch):
    """Build an API client with a spy MySQL adapter and writes disabled."""
    monkeypatch.setattr(constants, "GENERIC_MYSQL_WRITES_ENABLED", False)
    mysql = mock_db_manager.mysql_adapter
    mysql.find_paginated = Mock(return_value=([], 0))
    mysql.write = Mock()
    mysql.update = Mock()
    mysql.delete = Mock()
    api_module.db_manager = mock_db_manager
    yield TestClient(api_module.create_app())
    api_module.db_manager = None


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("post", "/api/v1/mysql/positions", {"data": {"position_id": "p1"}}),
        (
            "post",
            "/api/v1/mysql/ledger_adjustments",
            {"data": {"adjustment_id": "a1"}},
        ),
        (
            "put",
            "/api/v1/mysql/positions",
            {"filter": {"position_id": "p1"}, "data": {"status": "open"}},
        ),
        ("delete", "/api/v1/mysql/positions", {"filter": {"position_id": "p1"}}),
        (
            "post",
            "/api/v1/mysql/positions/batch",
            {"operations": [{"type": "insert", "data": {"position_id": "p1"}}]},
        ),
    ],
)
def test_generic_mysql_writes_are_rejected_before_adapter(
    client, method, path, payload
):
    response = client.request(method.upper(), path, json=payload)

    assert response.status_code == 403
    assert response.json() == {
        "detail": (
            "generic MySQL writes are disabled: MySQL is historic-only; "
            "use the typed API"
        )
    }
    mysql = api_module.db_manager.mysql_adapter
    mysql.write.assert_not_called()
    mysql.update.assert_not_called()
    mysql.delete.assert_not_called()


def test_legacy_mysql_insert_is_rejected(client):
    response = client.post(
        "/api/v1/data/insert",
        json={
            "database": "mysql",
            "collection": "positions",
            "records": [{"position_id": "p1"}],
        },
    )

    assert response.status_code == 403
    api_module.db_manager.mysql_adapter.write.assert_not_called()


def test_mysql_reads_remain_historic(client):
    response = client.get("/api/v1/mysql/klines_m15")

    assert response.status_code == 200
    assert response.headers["X-Petrosa-Store"] == "mysql-historic"


def test_mysql_legacy_query_remains_historic(client):
    response = client.post(
        "/api/v1/data/query",
        json={"database": "mysql", "collection": "klines_m15"},
    )

    assert response.status_code == 200
    assert response.headers["X-Petrosa-Store"] == "mysql-historic"


def test_enabled_flag_allows_legacy_insert_to_reach_adapter(client, monkeypatch):
    monkeypatch.setattr(constants, "GENERIC_MYSQL_WRITES_ENABLED", True)
    api_module.db_manager.mysql_adapter.write.return_value = Mock(
        inserted=1, duplicates=0, failed=0, ignored_count=0
    )

    response = client.post(
        "/api/v1/mysql/positions",
        json={"data": {"position_id": "p1"}},
    )

    assert response.status_code == 200
    api_module.db_manager.mysql_adapter.write.assert_called_once()
