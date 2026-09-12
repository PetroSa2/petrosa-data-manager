"""Tests for /catalog/datasets/{id}, /schemas/{id}, /lineage/{id} — closes #281.

Prior to this fix, these three routes returned 100% hardcoded stub data
regardless of `dataset_id`, even when `CatalogRepository.get_dataset()` was
a real, working, DB-backed method. These tests assert the routes now call
the real repository and return per-dataset data (or 404/503 when the
dataset/db is missing) instead of identical canned output for any id.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import data_manager.api.app as api_module
from data_manager.api.app import create_app

_DATASET_ROW = {
    "dataset_id": "ds-btcusdt-1h",
    "name": "BTCUSDT 1h Candles",
    "description": "Hourly OHLCV candles for BTCUSDT",
    "category": "market_data",
    "schema_id": "candles_schema_v2",
    "storage_type": "mongodb",
    "owner": "data-team",
    "update_frequency": "hourly",
    "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    "updated_at": datetime(2026, 2, 1, tzinfo=UTC),
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


def _wire_db_manager_with_dataset(dataset_row: dict | None):
    fake_manager = MagicMock()
    fake_manager.mysql_adapter = MagicMock()
    fake_manager.mongodb_adapter = MagicMock()
    fake_manager.mysql_adapter.query_latest.return_value = (
        [dataset_row] if dataset_row else []
    )
    api_module.db_manager = fake_manager
    return fake_manager


@pytest.mark.unit
def test_get_dataset_metadata_returns_real_data(client):
    _wire_db_manager_with_dataset(_DATASET_ROW)

    resp = client.get(f"/catalog/datasets/{_DATASET_ROW['dataset_id']}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["dataset_id"] == _DATASET_ROW["dataset_id"]
    assert body["name"] == _DATASET_ROW["name"]
    assert body["schema_id"] == _DATASET_ROW["schema_id"]
    assert body["storage_type"] == _DATASET_ROW["storage_type"]
    # No longer the old hardcoded stub values for any id.
    assert body["name"] != "Dataset Name"


@pytest.mark.unit
def test_get_dataset_metadata_404_when_missing(client):
    _wire_db_manager_with_dataset(None)

    resp = client.get("/catalog/datasets/does-not-exist")

    assert resp.status_code == 404


@pytest.mark.unit
def test_get_dataset_metadata_503_when_db_unavailable(client):
    api_module.db_manager = None

    resp = client.get("/catalog/datasets/anything")

    assert resp.status_code == 503


@pytest.mark.unit
def test_get_schema_returns_real_schema_id(client):
    _wire_db_manager_with_dataset(_DATASET_ROW)

    resp = client.get(f"/catalog/schemas/{_DATASET_ROW['dataset_id']}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["schema_id"] == _DATASET_ROW["schema_id"]


@pytest.mark.unit
def test_get_schema_404_when_dataset_missing(client):
    _wire_db_manager_with_dataset(None)

    resp = client.get("/catalog/schemas/does-not-exist")

    assert resp.status_code == 404


@pytest.mark.unit
def test_get_lineage_returns_per_dataset_metadata(client):
    _wire_db_manager_with_dataset(_DATASET_ROW)

    resp = client.get(f"/catalog/lineage/{_DATASET_ROW['dataset_id']}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["dataset_id"] == _DATASET_ROW["dataset_id"]
    assert body["metadata"]["last_updated"] == _DATASET_ROW["updated_at"].isoformat()


@pytest.mark.unit
def test_get_lineage_404_when_dataset_missing(client):
    _wire_db_manager_with_dataset(None)

    resp = client.get("/catalog/lineage/does-not-exist")

    assert resp.status_code == 404


@pytest.mark.unit
def test_different_dataset_ids_return_different_data(client):
    """Regression guard: the old stub returned identical output for any id."""
    row_a = dict(_DATASET_ROW, dataset_id="ds-a", name="Dataset A")
    row_b = dict(_DATASET_ROW, dataset_id="ds-b", name="Dataset B")

    _wire_db_manager_with_dataset(row_a)
    resp_a = client.get("/catalog/datasets/ds-a")

    _wire_db_manager_with_dataset(row_b)
    resp_b = client.get("/catalog/datasets/ds-b")

    assert resp_a.json()["name"] == "Dataset A"
    assert resp_b.json()["name"] == "Dataset B"
    assert resp_a.json()["name"] != resp_b.json()["name"]
