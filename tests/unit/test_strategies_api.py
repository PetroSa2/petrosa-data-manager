"""Tests for /api/strategies routes (#195, FR54).

Covers every AC of the strategy-registry leaf:

* AC — POST /api/strategies creates a candidate
* AC — POST 409 on duplicate strategy_id
* AC — GET /api/strategies/{id} returns the persisted document
* AC — GET 404 on missing id
* AC — GET /api/strategies lists with paging
* AC — GET /api/strategies filters by status
* AC — POST 422 when signed_action_id is missing (required-field validation)
* AC — code field is persisted verbatim and NEVER executed
* AC — POST 503 when MongoDB is unavailable
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import data_manager.api.app as api_module
from data_manager.api.app import create_app
from data_manager.db.repositories.strategy_registry_repository import (
    STRATEGY_REGISTRY_COLLECTION,
)

# ─── In-memory Mongo fake — minimal, mirrors the shape used by the strategy
#     registry repository (find_one by _id, find + sort, insert_one with
#     DuplicateKeyError on _id collision). ────────────────────────────────────


class _FakeCursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs
        self._sort_field: str | None = None
        self._sort_direction = 1
        self._iter: Any = None

    def sort(self, field, direction):
        self._sort_field = field
        self._sort_direction = direction
        return self

    def __aiter__(self):
        docs = [dict(d) for d in self._docs]
        if self._sort_field is not None:
            docs.sort(
                key=lambda d: d.get(self._sort_field, ""),
                reverse=self._sort_direction < 0,
            )
        self._iter = iter(docs)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration as e:
            raise StopAsyncIteration from e


def _matches(doc: dict, query: dict) -> bool:
    for k, v in query.items():
        if doc.get(k) != v:
            return False
    return True


class _FakeCollection:
    def __init__(self) -> None:
        self.docs: list[dict[str, Any]] = []

    async def find_one(self, query: dict):
        for d in self.docs:
            if _matches(d, query):
                return dict(d)
        return None

    def find(self, query: dict):
        matched = [d for d in self.docs if _matches(d, query)]
        return _FakeCursor(matched)

    async def insert_one(self, doc: dict):
        d = dict(doc)
        for existing in self.docs:
            if existing.get("_id") == d.get("_id"):
                from pymongo.errors import DuplicateKeyError

                raise DuplicateKeyError(f"duplicate key _id={d['_id']!r}")
        self.docs.append(d)

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
    """Wire the in-memory Mongo into the app module for the duration of a test."""
    original = api_module.db_manager
    api_module.db_manager = _FakeDbManager()
    try:
        app = create_app()
        client = TestClient(app)
        # Stash the mongo handle for in-test seeding/inspection.
        client.mongo = api_module.db_manager.mongodb_adapter  # type: ignore[attr-defined]
        yield client
    finally:
        api_module.db_manager = original


def _payload(**overrides: Any) -> dict[str, Any]:
    base = {
        "strategy_id": "momentum-v3",
        "code": "def signal(*a, **kw):\n    raise SystemExit('never executed')\n",
        "parameter_set": {"window": 14, "threshold": 0.03},
        "symbol_scope": ["BTCUSDT", "ETHUSDT"],
        "submitted_by": "alice",
        "signed_action_id": "sa-1",
    }
    base.update(overrides)
    return base


# ─── POST /api/strategies ────────────────────────────────────────────────────


def test_post_persists_candidate_and_returns_201(wired_app: TestClient) -> None:
    r = wired_app.post("/api/strategies", json=_payload())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["strategy_id"] == "momentum-v3"
    assert body["status"] == "candidate"
    assert "registered_at" in body and body["registered_at"]

    docs = wired_app.mongo.db[STRATEGY_REGISTRY_COLLECTION].docs
    assert len(docs) == 1
    stored = docs[0]
    assert stored["_id"] == "momentum-v3"
    assert stored["strategy_id"] == "momentum-v3"
    assert stored["status"] == "candidate"
    assert stored["submitted_by"] == "alice"
    assert stored["signed_action_id"] == "sa-1"
    assert stored["parameter_set"] == {"window": 14, "threshold": 0.03}
    assert stored["symbol_scope"] == ["BTCUSDT", "ETHUSDT"]
    # Code is persisted verbatim — never executed.
    assert "raise SystemExit" in stored["code"]


def test_post_duplicate_strategy_id_returns_409(wired_app: TestClient) -> None:
    r1 = wired_app.post("/api/strategies", json=_payload())
    assert r1.status_code == 201
    r2 = wired_app.post(
        "/api/strategies", json=_payload(submitted_by="bob", signed_action_id="sa-2")
    )
    assert r2.status_code == 409, r2.text
    body = r2.json()
    assert body["detail"]["strategy_id"] == "momentum-v3"
    # Only the first insert landed.
    assert len(wired_app.mongo.db[STRATEGY_REGISTRY_COLLECTION].docs) == 1


def test_post_missing_signed_action_id_returns_422(wired_app: TestClient) -> None:
    body = _payload()
    del body["signed_action_id"]
    r = wired_app.post("/api/strategies", json=body)
    assert r.status_code == 422


def test_post_empty_strategy_id_returns_422(wired_app: TestClient) -> None:
    r = wired_app.post("/api/strategies", json=_payload(strategy_id=""))
    assert r.status_code == 422


def test_post_empty_code_returns_422(wired_app: TestClient) -> None:
    r = wired_app.post("/api/strategies", json=_payload(code=""))
    assert r.status_code == 422


def test_post_503_when_mongo_unavailable() -> None:
    """No db_manager wired → 503 with structured detail."""
    original = api_module.db_manager
    api_module.db_manager = None
    try:
        app = create_app()
        client = TestClient(app)
        r = client.post("/api/strategies", json=_payload())
        assert r.status_code == 503
    finally:
        api_module.db_manager = original


def test_post_code_field_is_persisted_verbatim_not_executed(
    wired_app: TestClient,
) -> None:
    """Smoke test the AC: data-manager must not import/compile/exec the submitted
    code. A payload containing a SyntaxError-laden body must persist cleanly."""
    bad_code = "def signal(:\n    this is not python\n"
    r = wired_app.post(
        "/api/strategies", json=_payload(strategy_id="bad-syntax", code=bad_code)
    )
    assert r.status_code == 201, r.text
    stored = wired_app.mongo.db[STRATEGY_REGISTRY_COLLECTION].docs[0]
    assert stored["code"] == bad_code


# ─── GET /api/strategies/{strategy_id} ───────────────────────────────────────


def test_get_by_id_returns_persisted_document(wired_app: TestClient) -> None:
    wired_app.post("/api/strategies", json=_payload())
    r = wired_app.get("/api/strategies/momentum-v3")
    assert r.status_code == 200
    body = r.json()
    assert body["strategy_id"] == "momentum-v3"
    assert body["status"] == "candidate"
    assert body["submitted_by"] == "alice"
    assert body["signed_action_id"] == "sa-1"


def test_get_by_id_404_when_missing(wired_app: TestClient) -> None:
    r = wired_app.get("/api/strategies/does-not-exist")
    assert r.status_code == 404


# ─── GET /api/strategies (list) ──────────────────────────────────────────────


def test_list_empty(wired_app: TestClient) -> None:
    r = wired_app.get("/api/strategies")
    assert r.status_code == 200
    body = r.json()
    assert body["strategies"] == []
    assert body["count"] == 0
    assert body["status_filter"] is None


def test_list_returns_newest_first(wired_app: TestClient) -> None:
    for i in range(3):
        assert (
            wired_app.post(
                "/api/strategies",
                json=_payload(strategy_id=f"s-{i}", signed_action_id=f"sa-{i}"),
            ).status_code
            == 201
        )
    r = wired_app.get("/api/strategies")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 3
    ids = [s["strategy_id"] for s in body["strategies"]]
    # Newest first — s-2 was registered last.
    assert ids == ["s-2", "s-1", "s-0"]


def test_list_filters_by_status(wired_app: TestClient) -> None:
    # Seed two candidates and one manually-promoted accepted row.
    wired_app.post(
        "/api/strategies",
        json=_payload(strategy_id="cand-1", signed_action_id="sa-c1"),
    )
    wired_app.post(
        "/api/strategies",
        json=_payload(strategy_id="cand-2", signed_action_id="sa-c2"),
    )
    # Directly mutate the in-memory doc for the accepted case (status
    # transitions are out of scope of this leaf; we only test the read path).
    docs = wired_app.mongo.db[STRATEGY_REGISTRY_COLLECTION].docs
    docs.append(
        {
            "_id": "acc-1",
            "strategy_id": "acc-1",
            "code": "x = 1\n",
            "parameter_set": {},
            "symbol_scope": [],
            "submitted_by": "promoted",
            "signed_action_id": "sa-acc",
            "status": "accepted",
            "registered_at": "2025-01-01T00:00:00+00:00",
        }
    )

    r = wired_app.get("/api/strategies?status=candidate")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert all(s["status"] == "candidate" for s in body["strategies"])

    r2 = wired_app.get("/api/strategies?status=accepted")
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["count"] == 1
    assert body2["strategies"][0]["strategy_id"] == "acc-1"


def test_list_invalid_status_filter_returns_422(wired_app: TestClient) -> None:
    r = wired_app.get("/api/strategies?status=not-a-status")
    assert r.status_code == 422


def test_list_paging(wired_app: TestClient) -> None:
    for i in range(5):
        wired_app.post(
            "/api/strategies",
            json=_payload(strategy_id=f"p-{i}", signed_action_id=f"sa-p{i}"),
        )

    r = wired_app.get("/api/strategies?limit=2&offset=0")
    body = r.json()
    assert body["count"] == 2
    assert body["limit"] == 2
    assert body["offset"] == 0
    page1_ids = [s["strategy_id"] for s in body["strategies"]]

    r = wired_app.get("/api/strategies?limit=2&offset=2")
    body = r.json()
    assert body["count"] == 2
    page2_ids = [s["strategy_id"] for s in body["strategies"]]

    # No overlap between pages.
    assert set(page1_ids).isdisjoint(set(page2_ids))


def test_list_limit_bounds_enforced(wired_app: TestClient) -> None:
    r = wired_app.get("/api/strategies?limit=0")
    assert r.status_code == 422
    r = wired_app.get("/api/strategies?limit=10000")
    assert r.status_code == 422
    r = wired_app.get("/api/strategies?offset=-1")
    assert r.status_code == 422
