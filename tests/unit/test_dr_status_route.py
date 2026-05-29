"""Tests for /api/dashboard/dr-status (petrosa_k8s#743, P9-AC5.c)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import data_manager.api.app as api_module
from data_manager.api.app import create_app
from data_manager.api.routes.dr_status import RESTORE_EXERCISES_COLLECTION


class _FakeCursor:
    """Minimal motor-cursor shim — supports the same `sort` chaining the
    real route uses via the `find_one({}, sort=[(...)])` path. Most of
    the route's read path goes through `find_one`, not `find`, so the
    cursor surface stays small."""

    def __init__(self, docs):
        self._docs = docs

    def __aiter__(self):  # pragma: no cover — not used by dr-status route
        return iter(self._docs)


class _FakeCollection:
    def __init__(self) -> None:
        self.docs: list[dict] = []

    async def find_one(self, query: dict, sort=None):
        docs = list(self.docs)
        if sort:
            field, direction = sort[0]
            docs.sort(key=lambda d: d.get(field, 0), reverse=direction < 0)
        if not docs:
            return None
        # Mirror the route's filter (always empty {} here).
        return dict(docs[0])

    async def insert_one(self, doc: dict):
        doc = dict(doc)
        if "_id" not in doc:
            doc["_id"] = f"_autogen-{len(self.docs)}"
        self.docs.append(doc)


class _FakeDb:
    def __init__(self) -> None:
        self._collections: dict[str, _FakeCollection] = {}

    def __getitem__(self, name: str) -> _FakeCollection:
        return self._collections.setdefault(name, _FakeCollection())


class _FakeMongoAdapter:
    def __init__(self) -> None:
        self.db = _FakeDb()


class _FakeDbManager:
    def __init__(self) -> None:
        self.mongodb_adapter = _FakeMongoAdapter()
        self.mysql_adapter = None


@pytest.fixture
def wired_app():
    original = api_module.db_manager
    fake = _FakeDbManager()
    api_module.db_manager = fake
    try:
        yield fake
    finally:
        api_module.db_manager = original


@pytest.fixture
def client():
    return TestClient(create_app())


# ---------------------------------------------------------------------------
# Empty-collection case
# ---------------------------------------------------------------------------


def test_dr_status_returns_all_nulls_when_collection_empty(wired_app, client):
    resp = client.get("/api/dashboard/dr-status")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "last_exercise_at": None,
        "last_exercise_outcome": None,
        "days_since_last_exercise": None,
        "snapshot_id": None,
        "operator": None,
    }


# ---------------------------------------------------------------------------
# Happy path — recent pass row
# ---------------------------------------------------------------------------


def test_dr_status_returns_latest_pass(wired_app, client):
    fake_db = wired_app
    col = fake_db.mongodb_adapter.db[RESTORE_EXERCISES_COLLECTION]
    # Insert two rows; expect the newer one (sort=exercised_at desc).
    older = datetime(2026, 1, 15, 3, 0, tzinfo=UTC)
    newer = datetime(2026, 4, 15, 3, 0, tzinfo=UTC)
    import asyncio

    async def seed():
        await col.insert_one(
            {
                "exercised_at": older,
                "outcome": "pass",
                "snapshot_id": "audit-trail/2026-01-15-abc.archive.gz",
                "operator": "ops@petrosa",
            }
        )
        await col.insert_one(
            {
                "exercised_at": newer,
                "outcome": "pass",
                "snapshot_id": "audit-trail/2026-04-15-def.archive.gz",
                "operator": "restore-exercise-cronjob",
            }
        )

    asyncio.run(seed())

    resp = client.get("/api/dashboard/dr-status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["last_exercise_outcome"] == "pass"
    assert body["snapshot_id"] == "audit-trail/2026-04-15-def.archive.gz"
    assert body["operator"] == "restore-exercise-cronjob"
    assert body["last_exercise_at"] is not None
    assert body["last_exercise_at"].startswith("2026-04-15")
    assert isinstance(body["days_since_last_exercise"], int)
    assert body["days_since_last_exercise"] >= 0


def test_dr_status_returns_recent_fail(wired_app, client):
    """A failed exercise must surface as outcome='fail' so the dashboard
    can render the red badge that NFR-R6 AC5 calls for."""
    fake_db = wired_app
    col = fake_db.mongodb_adapter.db[RESTORE_EXERCISES_COLLECTION]
    import asyncio

    async def seed():
        await col.insert_one(
            {
                "exercised_at": datetime.now(UTC) - timedelta(days=5),
                "outcome": "fail",
                "snapshot_id": "audit-trail/2026-05-24-ghi.archive.gz",
                "operator": "restore-exercise-cronjob",
            }
        )

    asyncio.run(seed())

    resp = client.get("/api/dashboard/dr-status")
    body = resp.json()
    assert body["last_exercise_outcome"] == "fail"
    assert body["days_since_last_exercise"] == 5


# ---------------------------------------------------------------------------
# Cadence breach — last exercise older than NFR-R6 AC5's 90d window
# ---------------------------------------------------------------------------


def test_dr_status_reports_age_days_when_breaching_90d(wired_app, client):
    fake_db = wired_app
    col = fake_db.mongodb_adapter.db[RESTORE_EXERCISES_COLLECTION]
    import asyncio

    async def seed():
        await col.insert_one(
            {
                "exercised_at": datetime.now(UTC) - timedelta(days=120),
                "outcome": "pass",
                "snapshot_id": "audit-trail/stale.archive.gz",
                "operator": "ops",
            }
        )

    asyncio.run(seed())

    resp = client.get("/api/dashboard/dr-status")
    body = resp.json()
    # We don't break the contract over a stale exercise — that's the
    # dashboard's job to render. Just expose the truth.
    assert body["days_since_last_exercise"] >= 120
    assert body["last_exercise_outcome"] == "pass"


# ---------------------------------------------------------------------------
# Defensive — string-encoded `exercised_at` from an ad-hoc script
# ---------------------------------------------------------------------------


def test_dr_status_handles_iso_string_exercised_at(wired_app, client):
    fake_db = wired_app
    col = fake_db.mongodb_adapter.db[RESTORE_EXERCISES_COLLECTION]
    import asyncio

    async def seed():
        await col.insert_one(
            {
                "exercised_at": "2026-05-01T03:00:00Z",
                "outcome": "pass",
                "snapshot_id": "audit-trail/2026-05-01.archive.gz",
                "operator": "ad-hoc-script",
            }
        )

    asyncio.run(seed())

    resp = client.get("/api/dashboard/dr-status")
    body = resp.json()
    assert body["last_exercise_at"] is not None
    assert "2026-05-01" in body["last_exercise_at"]
    assert isinstance(body["days_since_last_exercise"], int)


def test_dr_status_handles_unparseable_exercised_at(wired_app, client):
    """An ad-hoc row with a malformed timestamp doesn't crash the
    dashboard — the field returns null, dashboard renders 'unknown'."""
    fake_db = wired_app
    col = fake_db.mongodb_adapter.db[RESTORE_EXERCISES_COLLECTION]
    import asyncio

    async def seed():
        await col.insert_one(
            {
                "exercised_at": "not-a-timestamp",
                "outcome": "pass",
                "snapshot_id": "audit-trail/broken.archive.gz",
                "operator": "broken-script",
            }
        )

    asyncio.run(seed())

    resp = client.get("/api/dashboard/dr-status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["last_exercise_at"] is None
    assert body["days_since_last_exercise"] is None
    # Other fields still surfaced — outcome + operator are not parsed.
    assert body["last_exercise_outcome"] == "pass"
    assert body["operator"] == "broken-script"


# ---------------------------------------------------------------------------
# 503 when MongoDB is absent
# ---------------------------------------------------------------------------


def test_dr_status_503_when_mongo_unavailable(client):
    original = api_module.db_manager
    api_module.db_manager = None
    try:
        resp = client.get("/api/dashboard/dr-status")
        assert resp.status_code == 503
        body = resp.json()
        assert "mongodb" in body["detail"]["title"].lower()
    finally:
        api_module.db_manager = original
