"""Tests for operator-configurable leverage bounds + versioning (#182, FR61)."""

from __future__ import annotations

from datetime import UTC, datetime, timezone

import pytest

from data_manager.db.repositories.leverage_bounds_repository import (
    LeverageBoundsRepository,
)
from data_manager.models.leverage_bounds import (
    LeverageBounds,
    LeverageBoundsDiff,
    LeverageBoundsPutRequest,
)

# ---------------------------------------------------------------------------
# Model — LeverageBounds + LeverageBoundsPutRequest
# ---------------------------------------------------------------------------


def test_leverage_bounds_doc_id_uses_v_prefix():
    bounds = LeverageBounds(
        version=7,
        per_strategy={"momentum-v3": 2},
        aggregate_ceiling=3.0,
        changed_by="ops@petrosa",
        reason="raise momentum cap",
    )
    assert bounds.doc_id() == "v7"


def test_leverage_bounds_validates_version_min():
    with pytest.raises(ValueError) as exc:
        LeverageBounds(
            version=0,
            per_strategy={},
            aggregate_ceiling=1.0,
            changed_by="ops",
            reason="x",
        )
    assert "version" in str(exc.value)


def test_leverage_bounds_validates_aggregate_ceiling_non_negative():
    with pytest.raises(ValueError) as exc:
        LeverageBounds(
            version=1,
            per_strategy={},
            aggregate_ceiling=-0.1,
            changed_by="ops",
            reason="x",
        )
    assert "aggregate_ceiling" in str(exc.value).lower()


def test_leverage_bounds_validates_changed_by_and_reason():
    with pytest.raises(ValueError) as missing_changed_by:
        LeverageBounds(
            version=1,
            per_strategy={},
            aggregate_ceiling=1.0,
            changed_by="",
            reason="x",
        )
    assert "changed_by" in str(missing_changed_by.value)

    with pytest.raises(ValueError) as missing_reason:
        LeverageBounds(
            version=1,
            per_strategy={},
            aggregate_ceiling=1.0,
            changed_by="ops",
            reason="",
        )
    assert "reason" in str(missing_reason.value)


def test_leverage_bounds_put_request_round_trips():
    req = LeverageBoundsPutRequest(
        per_strategy={"momentum-v3": 2, "meanrev-v1": 1},
        aggregate_ceiling=3.5,
        changed_by="ops@petrosa",
        reason="opening session retune",
    )
    assert req.per_strategy["momentum-v3"] == 2
    assert req.aggregate_ceiling == 3.5
    assert "retune" in req.reason


# ---------------------------------------------------------------------------
# Diff — AC6.d audit trail format
# ---------------------------------------------------------------------------


def _bounds(
    version: int,
    per_strategy: dict[str, int],
    aggregate: float,
) -> LeverageBounds:
    return LeverageBounds(
        version=version,
        per_strategy=per_strategy,
        aggregate_ceiling=aggregate,
        changed_by="ops",
        reason="test",
        changed_at=datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC),
    )


def test_diff_first_version_reports_all_as_added():
    cur = _bounds(1, {"s1": 2, "s2": 3}, 5.0)
    diff = LeverageBoundsDiff.compute(previous=None, current=cur)

    assert diff.from_version is None
    assert diff.to_version == 1
    assert diff.per_strategy_added == {"s1": 2, "s2": 3}
    assert diff.per_strategy_removed == {}
    assert diff.per_strategy_changed == {}
    assert diff.aggregate_ceiling_changed == (0.0, 5.0)
    assert not diff.is_noop()


def test_diff_detects_added_removed_changed():
    prev = _bounds(1, {"s1": 2, "s2": 3}, 5.0)
    cur = _bounds(2, {"s1": 4, "s3": 1}, 5.0)  # s1 changed, s2 removed, s3 added
    diff = LeverageBoundsDiff.compute(previous=prev, current=cur)

    assert diff.from_version == 1
    assert diff.to_version == 2
    assert diff.per_strategy_added == {"s3": 1}
    assert diff.per_strategy_removed == {"s2": 3}
    assert diff.per_strategy_changed == {"s1": (2, 4)}
    assert diff.aggregate_ceiling_changed is None  # unchanged


def test_diff_detects_aggregate_ceiling_change():
    prev = _bounds(1, {"s1": 2}, 5.0)
    cur = _bounds(2, {"s1": 2}, 7.5)
    diff = LeverageBoundsDiff.compute(previous=prev, current=cur)
    assert diff.aggregate_ceiling_changed == (5.0, 7.5)
    assert not diff.is_noop()


def test_diff_noop_when_nothing_changed():
    prev = _bounds(1, {"s1": 2}, 5.0)
    cur = _bounds(2, {"s1": 2}, 5.0)
    diff = LeverageBoundsDiff.compute(previous=prev, current=cur)
    assert diff.is_noop()


# ---------------------------------------------------------------------------
# Repository — fake in-memory Mongo collection
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, docs: list[dict]) -> None:
        self._docs = docs
        self._sort_field: str | None = None
        self._sort_direction = 1
        self._limit: int | None = None
        self._projection: dict | None = None

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
            drop = {k for k, v in self._projection.items() if v == 0}
            shaped: list[dict] = []
            for d in docs:
                row = {k: v for k, v in d.items() if k in keep}
                for k in drop:
                    row.pop(k, None)
                shaped.append(row)
            docs = shaped
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
        for d in self.docs:
            if d.get("_id") == doc.get("_id"):
                raise RuntimeError(
                    f"duplicate key _id={doc.get('_id')!r}"
                )  # mimics Mongo
        self.docs.append(dict(doc))


class _FakeDb:
    def __init__(self, col: _FakeCollection) -> None:
        self._col = col

    def __getitem__(self, _name: str) -> _FakeCollection:
        return self._col


class _FakeMongoAdapter:
    def __init__(self) -> None:
        self._col = _FakeCollection()
        self.db = _FakeDb(self._col)

    @property
    def collection(self) -> _FakeCollection:
        return self._col


@pytest.fixture
def repo_and_collection():
    mongo = _FakeMongoAdapter()
    repo = LeverageBoundsRepository(mongodb_adapter=mongo)
    return repo, mongo.collection


@pytest.mark.asyncio
async def test_repository_get_latest_returns_none_when_empty(repo_and_collection):
    repo, _ = repo_and_collection
    assert await repo.get_latest() is None


@pytest.mark.asyncio
async def test_repository_insert_next_starts_at_version_1(repo_and_collection):
    repo, col = repo_and_collection
    bounds = LeverageBounds(
        version=1,  # will be overridden by insert_next
        per_strategy={"s1": 2},
        aggregate_ceiling=3.0,
        changed_by="ops",
        reason="initial",
    )

    inserted = await repo.insert_next(bounds)
    assert inserted.version == 1
    assert col.docs[0]["_id"] == "v1"


@pytest.mark.asyncio
async def test_repository_insert_next_monotonic(repo_and_collection):
    repo, _ = repo_and_collection

    await repo.insert_next(
        LeverageBounds(
            version=1,
            per_strategy={"s1": 2},
            aggregate_ceiling=3.0,
            changed_by="ops",
            reason="v1",
        )
    )
    second = await repo.insert_next(
        LeverageBounds(
            version=999,  # caller's value MUST be overridden
            per_strategy={"s1": 4},
            aggregate_ceiling=3.0,
            changed_by="ops",
            reason="v2",
        )
    )
    third = await repo.insert_next(
        LeverageBounds(
            version=1,
            per_strategy={"s1": 4, "s2": 1},
            aggregate_ceiling=4.0,
            changed_by="ops",
            reason="v3",
        )
    )
    assert second.version == 2
    assert third.version == 3


@pytest.mark.asyncio
async def test_repository_get_latest_returns_highest_version(repo_and_collection):
    repo, _ = repo_and_collection

    for n in range(1, 4):
        await repo.insert_next(
            LeverageBounds(
                version=1,
                per_strategy={"s1": n},
                aggregate_ceiling=float(n),
                changed_by="ops",
                reason=f"v{n}",
            )
        )

    latest = await repo.get_latest()
    assert latest is not None
    assert latest.version == 3
    assert latest.aggregate_ceiling == 3.0


@pytest.mark.asyncio
async def test_repository_get_version_exact_lookup(repo_and_collection):
    repo, _ = repo_and_collection
    for n in range(1, 4):
        await repo.insert_next(
            LeverageBounds(
                version=1,
                per_strategy={"s1": n},
                aggregate_ceiling=float(n),
                changed_by="ops",
                reason=f"v{n}",
            )
        )

    v2 = await repo.get_version(2)
    assert v2 is not None and v2.version == 2 and v2.per_strategy == {"s1": 2}

    assert await repo.get_version(99) is None
    assert await repo.get_version(0) is None


@pytest.mark.asyncio
async def test_repository_list_versions_descending(repo_and_collection):
    repo, _ = repo_and_collection
    for _ in range(5):
        await repo.insert_next(
            LeverageBounds(
                version=1,
                per_strategy={},
                aggregate_ceiling=1.0,
                changed_by="ops",
                reason="seed",
            )
        )

    versions = await repo.list_versions()
    assert versions == [5, 4, 3, 2, 1]


def test_repository_id_to_version_rejects_bad_shape():
    with pytest.raises(ValueError) as no_prefix:
        LeverageBoundsRepository._id_to_version("123")
    assert "shape" in str(no_prefix.value)

    with pytest.raises(ValueError) as bad_int:
        LeverageBoundsRepository._id_to_version("vNaN")
    assert "shape" in str(bad_int.value)


def test_repository_id_to_version_round_trips():
    assert LeverageBoundsRepository._id_to_version("v1") == 1
    assert LeverageBoundsRepository._id_to_version("v42") == 42
