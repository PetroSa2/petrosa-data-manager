"""Tests for ``POST /api/v1/characterizations`` (petrosa-data-manager#202, FR54-B precursor).

Covers:
* Happy-path persist returns 201 with the persisted document
* Idempotent re-POST keyed by (strategy_id, strategy_version) replaces in place
* 422 when ``metrics`` is missing one of (sharpe, win_rate, mean_return)
* 422 on Pydantic schema validation (missing required body field)
* 503 when MongoDB is unavailable
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import data_manager.api.app as api_module
from data_manager.api.app import create_app
from data_manager.db.repositories.characterization_repository import (
    CHARACTERIZATIONS_COLLECTION,
)

# ─── In-memory Mongo fake with replace_one (the upsert path) ────────────────


class _FakeCollection:
    def __init__(self) -> None:
        self.docs: list[dict[str, Any]] = []

    async def find_one(self, query: dict):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                return dict(d)
        return None

    async def replace_one(self, query: dict, replacement: dict, upsert: bool = False):
        for i, d in enumerate(self.docs):
            if all(d.get(k) == v for k, v in query.items()):
                self.docs[i] = dict(replacement)
                return
        if upsert:
            self.docs.append(dict(replacement))

    async def create_index(self, *args, **kwargs):  # noqa: ARG002
        return None


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
    api_module.db_manager = _FakeDbManager()
    try:
        app = create_app()
        client = TestClient(app)
        client.mongo = api_module.db_manager.mongodb_adapter  # type: ignore[attr-defined]
        yield client
    finally:
        api_module.db_manager = original


def _payload(**overrides: Any) -> dict[str, Any]:
    base = {
        "strategy_id": "momentum-v3",
        "strategy_version": "v1.0.0",
        "data_window_from": "2026-05-01T00:00:00Z",
        "data_window_to": "2026-05-15T00:00:00Z",
        "seed": 42,
        "metrics": {"sharpe": 1.5, "win_rate": 0.55, "mean_return": 0.012},
        "drawdown_envelope": [0.05, 0.10, 0.15, 0.20],
        "inputs_hash": "a" * 64,
    }
    base.update(overrides)
    return base


# ─── happy path ──────────────────────────────────────────────────────────────


def test_post_persists_artifact_and_returns_201(wired_app: TestClient) -> None:
    r = wired_app.post("/api/v1/characterizations", json=_payload())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["strategy_id"] == "momentum-v3"
    assert body["strategy_version"] == "v1.0.0"
    assert body["metrics"]["sharpe"] == 1.5
    docs = wired_app.mongo.db[CHARACTERIZATIONS_COLLECTION].docs
    assert len(docs) == 1


# ─── idempotent re-POST ──────────────────────────────────────────────────────


def test_re_post_same_key_replaces_in_place(wired_app: TestClient) -> None:
    r1 = wired_app.post("/api/v1/characterizations", json=_payload())
    assert r1.status_code == 201
    updated = _payload()
    updated["metrics"] = {"sharpe": 2.0, "win_rate": 0.60, "mean_return": 0.020}
    r2 = wired_app.post("/api/v1/characterizations", json=updated)
    assert r2.status_code == 201
    docs = wired_app.mongo.db[CHARACTERIZATIONS_COLLECTION].docs
    assert len(docs) == 1
    assert docs[0]["metrics"]["sharpe"] == 2.0


def test_different_strategy_version_creates_new_doc(wired_app: TestClient) -> None:
    a = wired_app.post(
        "/api/v1/characterizations", json=_payload(strategy_version="v1")
    )
    b = wired_app.post(
        "/api/v1/characterizations", json=_payload(strategy_version="v2")
    )
    assert a.status_code == 201
    assert b.status_code == 201
    docs = wired_app.mongo.db[CHARACTERIZATIONS_COLLECTION].docs
    assert len(docs) == 2


# ─── 422 — missing required metric key ──────────────────────────────────────


def test_post_returns_422_when_metrics_missing_sharpe(wired_app: TestClient) -> None:
    body = _payload(metrics={"win_rate": 0.5, "mean_return": 0.01})  # no sharpe
    r = wired_app.post("/api/v1/characterizations", json=body)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "sharpe" in str(detail)


def test_post_returns_422_when_all_required_metrics_missing(
    wired_app: TestClient,
) -> None:
    body = _payload(metrics={"other": 1.0})
    r = wired_app.post("/api/v1/characterizations", json=body)
    assert r.status_code == 422


# ─── 422 — Pydantic body schema ─────────────────────────────────────────────


def test_post_returns_422_when_required_body_field_missing(
    wired_app: TestClient,
) -> None:
    body = _payload()
    body.pop("inputs_hash")
    r = wired_app.post("/api/v1/characterizations", json=body)
    assert r.status_code == 422


# ─── 503 — Mongo unavailable ────────────────────────────────────────────────


def test_post_returns_503_when_mongo_unavailable() -> None:
    original = api_module.db_manager
    api_module.db_manager = None
    try:
        app = create_app()
        client = TestClient(app)
        r = client.post("/api/v1/characterizations", json=_payload())
        assert r.status_code == 503
        assert r.json()["detail"]["title"] == "data-manager in limited mode"
    finally:
        api_module.db_manager = original
