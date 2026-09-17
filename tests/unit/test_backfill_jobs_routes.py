"""Tests for GET /backfill/jobs and /backfill/jobs/{job_id} — closes #281.

Prior to this fix, `list_backfill_jobs` always returned an empty hardcoded
list and `get_backfill_job` fabricated a `status="completed"` response for
any `job_id`, even though `BackfillRepository.get_job()` was real and
DB-backed. These tests assert the routes now call the real repository.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import data_manager.api.app as api_module
from data_manager.api.app import create_app

_JOB_ROW = {
    "job_id": "job-real-123",
    "symbol": "ETHUSDT",
    "data_type": "candles",
    "timeframe": "1h",
    "start_time": datetime(2026, 1, 1, tzinfo=UTC),
    "end_time": datetime(2026, 1, 2, tzinfo=UTC),
    "status": "running",
    "progress": 42.5,
    "records_fetched": 500,
    "records_inserted": 480,
    "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    "started_at": datetime(2026, 1, 1, 0, 5, tzinfo=UTC),
    "completed_at": None,
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
def test_get_backfill_job_returns_real_data(client):
    fake_manager = MagicMock()
    fake_manager.mysql_adapter = MagicMock()
    fake_manager.mongodb_adapter = MagicMock()

    from data_manager.db.repositories import BackfillRepository

    real_get_job = BackfillRepository.get_job

    async def fake_get_job(self, job_id):
        return _JOB_ROW if job_id == _JOB_ROW["job_id"] else None

    BackfillRepository.get_job = fake_get_job
    api_module.db_manager = fake_manager
    try:
        resp = client.get(f"/backfill/jobs/{_JOB_ROW['job_id']}")
    finally:
        BackfillRepository.get_job = real_get_job

    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == _JOB_ROW["job_id"]
    assert body["status"] == "running"
    assert body["request"]["symbol"] == "ETHUSDT"
    # No longer the old hardcoded "completed"/BTCUSDT stub for any id.
    assert body["status"] != "completed" or body["request"]["symbol"] != "BTCUSDT"


@pytest.mark.unit
def test_get_backfill_job_404_when_missing(client):
    fake_manager = MagicMock()
    fake_manager.mysql_adapter = MagicMock()
    fake_manager.mongodb_adapter = MagicMock()
    api_module.db_manager = fake_manager

    from data_manager.db.repositories import BackfillRepository

    real_get_job = BackfillRepository.get_job

    async def fake_get_job(self, job_id):
        return None

    BackfillRepository.get_job = fake_get_job
    try:
        resp = client.get("/backfill/jobs/does-not-exist")
    finally:
        BackfillRepository.get_job = real_get_job

    assert resp.status_code == 404


@pytest.mark.unit
def test_get_backfill_job_503_when_db_unavailable(client):
    api_module.db_manager = None

    resp = client.get("/backfill/jobs/anything")

    assert resp.status_code == 503


@pytest.mark.unit
def test_list_backfill_jobs_returns_real_data(client):
    fake_manager = MagicMock()
    fake_manager.mysql_adapter = MagicMock()
    fake_manager.mongodb_adapter = MagicMock()
    api_module.db_manager = fake_manager

    from data_manager.db.repositories import BackfillRepository

    real_list_jobs = BackfillRepository.list_jobs
    BackfillRepository.list_jobs = lambda self, **kwargs: ([_JOB_ROW], 1)
    try:
        resp = client.get("/backfill/jobs")
    finally:
        BackfillRepository.list_jobs = real_list_jobs

    assert resp.status_code == 200
    body = resp.json()
    assert body["pagination"]["total"] == 1
    assert len(body["data"]) == 1
    assert body["data"][0]["job_id"] == _JOB_ROW["job_id"]


@pytest.mark.unit
def test_list_backfill_jobs_empty_when_db_unavailable(client):
    api_module.db_manager = None

    resp = client.get("/backfill/jobs")

    assert resp.status_code == 200
    body = resp.json()
    assert body["data"] == []
    assert body["pagination"]["total"] == 0
