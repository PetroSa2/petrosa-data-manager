"""Tests for /api/dashboard/leverage-bounds routes (#182, AC6.b/c/d wiring)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import data_manager.api.app as api_module
from data_manager.api.app import create_app
from data_manager.api.routes.leverage_bounds import (
    LEVERAGE_BOUNDS_AUDIT_COLLECTION,
    LEVERAGE_BOUNDS_NATS_SUBJECT,
    set_leverage_bounds_publisher,
)

# ---------------------------------------------------------------------------
# Fake Mongo + DB manager — same shape as test_leverage_bounds.py but with
# audit-trail collection support.
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, docs):
        self._docs = docs
        self._sort_field = None
        self._sort_direction = 1
        self._limit = None
        self._projection = None

    def sort(self, field, direction):
        self._sort_field = field
        self._sort_direction = direction
        return self

    def limit(self, n):
        self._limit = n
        return self

    def __aiter__(self):
        docs = list(self._docs)
        if self._sort_field is not None:
            docs.sort(
                key=lambda d: d.get(self._sort_field, 0),
                reverse=self._sort_direction < 0,
            )
        if self._limit is not None:
            docs = docs[: self._limit]
        if self._projection is not None:
            keep = {k for k, v in self._projection.items() if v}
            docs = [{k: v for k, v in d.items() if k in keep} for d in docs]
        self._iter = iter(docs)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration as e:
            raise StopAsyncIteration from e


class _FakeCollection:
    def __init__(self) -> None:
        self.docs: list[dict] = []

    async def find_one(self, query: dict, sort=None):
        if "_id" in query:
            for d in self.docs:
                if d.get("_id") == query["_id"]:
                    return dict(d)
            return None
        docs = list(self.docs)
        if sort:
            field, direction = sort[0]
            docs.sort(key=lambda d: d.get(field, 0), reverse=direction < 0)
        return dict(docs[0]) if docs else None

    def find(self, query: dict, projection=None):
        cursor = _FakeCursor(list(self.docs))
        cursor._projection = projection
        return cursor

    async def insert_one(self, doc: dict):
        doc = dict(doc)
        # Real Mongo auto-generates ObjectId when _id is absent; simulate that
        # so callers writing audit-style docs without a stable _id work.
        if "_id" not in doc:
            doc["_id"] = f"_autogen-{len(self.docs)}"
        for d in self.docs:
            if d.get("_id") == doc.get("_id"):
                raise RuntimeError(f"duplicate key _id={doc.get('_id')!r}")
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


class _RecordingPublisher:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, subject: str, payload: bytes) -> None:
        self.published.append((subject, json.loads(payload.decode())))


@pytest.fixture
def wired_app():
    """Wires the in-memory Mongo + recording NATS publisher into the app module."""
    original_db_manager = api_module.db_manager
    fake_db = _FakeDbManager()
    api_module.db_manager = fake_db
    publisher = _RecordingPublisher()
    set_leverage_bounds_publisher(publisher)
    try:
        yield fake_db, publisher
    finally:
        api_module.db_manager = original_db_manager
        set_leverage_bounds_publisher(None)


@pytest.fixture
def client():
    return TestClient(create_app())


# ---------------------------------------------------------------------------
# GET — empty / not-configured
# ---------------------------------------------------------------------------


def test_get_latest_returns_404_when_empty(wired_app, client):
    resp = client.get("/api/dashboard/leverage-bounds")
    assert resp.status_code == 404
    body = resp.json()
    assert "leverage_bounds" in body["detail"]["title"]


def test_get_version_returns_404_for_missing(wired_app, client):
    resp = client.get("/api/dashboard/leverage-bounds/42")
    assert resp.status_code == 404


def test_get_version_validates_min_version(wired_app, client):
    resp = client.get("/api/dashboard/leverage-bounds/0")
    # FastAPI Path(ge=1) → 422
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# PUT happy path — full AC6.a/b/c/d roundtrip
# ---------------------------------------------------------------------------


def test_put_writes_version_1_with_full_side_effects(wired_app, client):
    fake_db, publisher = wired_app
    payload = {
        "per_strategy": {"momentum-v3": 2, "meanrev-v1": 1},
        "aggregate_ceiling": 3.5,
        "changed_by": "ops@petrosa",
        "reason": "initial bounds",
    }

    resp = client.put("/api/dashboard/leverage-bounds", json=payload)
    assert resp.status_code == 200
    body = resp.json()

    # AC6.a — storage at v1
    assert body["bounds"]["version"] == 1
    assert body["bounds"]["per_strategy"] == {"momentum-v3": 2, "meanrev-v1": 1}
    assert body["bounds"]["aggregate_ceiling"] == 3.5

    # AC6.d — diff carries all-as-added (no previous version)
    assert body["diff"]["from_version"] is None
    assert body["diff"]["to_version"] == 1
    assert body["diff"]["per_strategy_added"] == {
        "momentum-v3": 2,
        "meanrev-v1": 1,
    }
    assert body["diff"]["aggregate_ceiling_changed"] == [0.0, 3.5]

    # AC6.d wiring — audit-trail row persisted.
    audit_col = fake_db.mongodb_adapter.db[LEVERAGE_BOUNDS_AUDIT_COLLECTION]
    assert len(audit_col.docs) == 1
    audit_doc = audit_col.docs[0]
    assert audit_doc["kind"] == "leverage_bounds_update"
    assert audit_doc["to_version"] == 1
    assert audit_doc["from_version"] is None
    assert audit_doc["changed_by"] == "ops@petrosa"
    assert audit_doc["reason"] == "initial bounds"

    # AC6.c — NATS publish.
    assert body["nats_published"] is True
    assert len(publisher.published) == 1
    subject, msg = publisher.published[0]
    assert subject == LEVERAGE_BOUNDS_NATS_SUBJECT
    assert msg["version"] == 1
    assert msg["aggregate_ceiling"] == 3.5


def test_put_second_version_diffs_against_first(wired_app, client):
    fake_db, publisher = wired_app

    # v1
    client.put(
        "/api/dashboard/leverage-bounds",
        json={
            "per_strategy": {"s1": 2},
            "aggregate_ceiling": 5.0,
            "changed_by": "ops",
            "reason": "v1",
        },
    )
    # v2 — s1 cap changes; aggregate unchanged.
    resp = client.put(
        "/api/dashboard/leverage-bounds",
        json={
            "per_strategy": {"s1": 4, "s2": 1},
            "aggregate_ceiling": 5.0,
            "changed_by": "ops",
            "reason": "v2",
        },
    )
    body = resp.json()
    assert body["bounds"]["version"] == 2
    assert body["diff"]["per_strategy_changed"] == {"s1": [2, 4]}
    assert body["diff"]["per_strategy_added"] == {"s2": 1}
    assert body["diff"]["per_strategy_removed"] == {}
    assert body["diff"]["aggregate_ceiling_changed"] is None

    audit_col = fake_db.mongodb_adapter.db[LEVERAGE_BOUNDS_AUDIT_COLLECTION]
    assert len(audit_col.docs) == 2
    assert publisher.published[-1][1]["version"] == 2


def test_put_validates_negative_aggregate_ceiling(wired_app, client):
    resp = client.put(
        "/api/dashboard/leverage-bounds",
        json={
            "per_strategy": {},
            "aggregate_ceiling": -0.5,
            "changed_by": "ops",
            "reason": "bad",
        },
    )
    assert resp.status_code == 422


def test_put_validates_missing_changed_by(wired_app, client):
    resp = client.put(
        "/api/dashboard/leverage-bounds",
        json={
            "per_strategy": {},
            "aggregate_ceiling": 1.0,
            "changed_by": "",
            "reason": "bad",
        },
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET — after PUT
# ---------------------------------------------------------------------------


def test_get_latest_returns_most_recent_put(wired_app, client):
    client.put(
        "/api/dashboard/leverage-bounds",
        json={
            "per_strategy": {"s1": 2},
            "aggregate_ceiling": 5.0,
            "changed_by": "ops",
            "reason": "v1",
        },
    )
    client.put(
        "/api/dashboard/leverage-bounds",
        json={
            "per_strategy": {"s1": 3},
            "aggregate_ceiling": 5.0,
            "changed_by": "ops",
            "reason": "v2",
        },
    )
    resp = client.get("/api/dashboard/leverage-bounds")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == 2
    assert body["per_strategy"] == {"s1": 3}


def test_get_version_exact_lookup(wired_app, client):
    client.put(
        "/api/dashboard/leverage-bounds",
        json={
            "per_strategy": {"s1": 2},
            "aggregate_ceiling": 5.0,
            "changed_by": "ops",
            "reason": "v1",
        },
    )
    client.put(
        "/api/dashboard/leverage-bounds",
        json={
            "per_strategy": {"s1": 4},
            "aggregate_ceiling": 5.0,
            "changed_by": "ops",
            "reason": "v2",
        },
    )

    v1_resp = client.get("/api/dashboard/leverage-bounds/1")
    assert v1_resp.status_code == 200
    assert v1_resp.json()["per_strategy"] == {"s1": 2}

    v2_resp = client.get("/api/dashboard/leverage-bounds/2")
    assert v2_resp.json()["per_strategy"] == {"s1": 4}


def test_list_versions_returns_descending(wired_app, client):
    for n in range(1, 4):
        client.put(
            "/api/dashboard/leverage-bounds",
            json={
                "per_strategy": {},
                "aggregate_ceiling": float(n),
                "changed_by": "ops",
                "reason": f"v{n}",
            },
        )
    resp = client.get("/api/dashboard/leverage-bounds/versions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["versions"] == [3, 2, 1]
    assert body["count"] == 3


# ---------------------------------------------------------------------------
# NATS publisher failure — must not break the route
# ---------------------------------------------------------------------------


def test_put_succeeds_when_nats_unavailable(wired_app, client):
    """No publisher wired → PUT still writes storage + audit and returns nats_published=False."""
    set_leverage_bounds_publisher(None)
    fake_db, _ = wired_app
    resp = client.put(
        "/api/dashboard/leverage-bounds",
        json={
            "per_strategy": {"s1": 2},
            "aggregate_ceiling": 5.0,
            "changed_by": "ops",
            "reason": "nats-down",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["nats_published"] is False
    audit_col = fake_db.mongodb_adapter.db[LEVERAGE_BOUNDS_AUDIT_COLLECTION]
    assert len(audit_col.docs) == 1


def test_put_continues_when_publisher_raises(wired_app, client):
    """A broken publisher must not break the operator-facing PUT."""

    class _BrokenPublisher:
        async def publish(self, subject, payload):
            raise RuntimeError("nats down")

    set_leverage_bounds_publisher(_BrokenPublisher())
    resp = client.put(
        "/api/dashboard/leverage-bounds",
        json={
            "per_strategy": {"s1": 2},
            "aggregate_ceiling": 5.0,
            "changed_by": "ops",
            "reason": "nats-broken",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["nats_published"] is False
