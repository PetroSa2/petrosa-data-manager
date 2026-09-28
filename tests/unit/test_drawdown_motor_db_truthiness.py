"""Regression: ``drawdown_cycle_strategy_failed`` storm in production.

Root cause: ``CharacterizationRepository`` (and three sibling repositories)
guarded Mongo access with ``not getattr(self.mongodb, "db", None)``. The
adapter's ``db`` is a Motor ``AsyncIOMotorDatabase``, which raises
``NotImplementedError`` from ``__bool__`` ("Database objects do not
implement truth value testing or bool()"). Every
``DrawdownService.compute`` -> ``_fetch_envelope`` -> ``get_latest`` call
therefore raised, and ``DrawdownScheduler.run_cycle`` logged
``drawdown_cycle_strategy_failed`` once per strategy per tick — with no
detail, because the log format is ``%(message)s`` and the detail lived in
``extra``.

The pre-existing tests used ``MagicMock`` databases (truthy), so they
never exercised the Motor contract. These tests use a stand-in that
reproduces Motor's ``__bool__`` behaviour, and one test pins that contract
against the real Motor class.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from data_manager.db.repositories.characterization_repository import (
    CharacterizationRepository,
)
from data_manager.db.repositories.lifecycle_repository import LifecycleRepository
from data_manager.db.repositories.strategy_lifecycle_repository import (
    StrategyLifecycleRepository,
)
from data_manager.db.repositories.strategy_timeline_repository import (
    StrategyTimelineRepository,
)
from data_manager.portfolio import drawdown_scheduler as scheduler_module
from data_manager.portfolio.drawdown_scheduler import (
    DrawdownScheduler,
    _describe_cause,
)

NOW = datetime(2026, 9, 28, 19, 0, tzinfo=UTC)


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    async def to_list(self, length=None):
        return list(self._docs)


class _MotorLikeDatabase:
    """Mimics ``AsyncIOMotorDatabase``: indexable, but ``bool()`` raises."""

    def __init__(self, collections: dict[str, list[dict]] | None = None):
        self._collections = collections or {}
        self.accessed: list[str] = []

    def __bool__(self):
        raise NotImplementedError(
            "Database objects do not implement truth value testing or bool(). "
            "Please compare with None instead: database is not None"
        )

    def __getitem__(self, name):
        self.accessed.append(name)
        coll = MagicMock()
        docs = self._collections.get(name, [])
        coll.find = MagicMock(return_value=_Cursor(docs))
        coll.find_one = AsyncMock(return_value=docs[0] if docs else None)
        coll.replace_one = AsyncMock()
        return coll


def _adapter(collections=None):
    adapter = MagicMock()
    adapter._connected = True
    adapter.db = _MotorLikeDatabase(collections)
    return adapter


def _characterization_doc(strategy_id: str, envelope: list[float]) -> dict:
    return {
        "_id": f"{strategy_id}::v1",
        "strategy_id": strategy_id,
        "strategy_version": "v1",
        "data_window_from": NOW - timedelta(days=30),
        "data_window_to": NOW - timedelta(days=1),
        "seed": 42,
        "metrics": {"sharpe": 1.2, "win_rate": 0.55, "mean_return": 0.001},
        "drawdown_envelope": envelope,
        "inputs_hash": "abc123",
        "created_at": NOW,
    }


def test_real_motor_database_forbids_truth_testing():
    """Pin the upstream contract the bug depended on."""
    from motor.motor_asyncio import AsyncIOMotorClient

    client = AsyncIOMotorClient(
        "mongodb://127.0.0.1:1", connect=False, serverSelectionTimeoutMS=1
    )
    try:
        db = client["petrosa"]
        with pytest.raises(NotImplementedError):
            bool(db)
        assert db is not None
    finally:
        client.close()


# ---------------------------------------------------------------------------
# End-to-end reproduction of the production failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_cycle_with_real_characterization_repo(caplog):
    """The production wiring: real repo + Motor-like db, several strategies."""
    now = datetime.now(UTC)
    adapter = _adapter(
        {
            "characterizations": [
                _characterization_doc("ta-momentum", [5.0, 10.0, 20.0, 30.0])
            ],
            "pnl_events": [
                {
                    "strategy_id": "ta-momentum",
                    "pnl_kind": "closed",
                    "realized_pnl_usd": 100.0,
                    "timestamp": now,
                },
            ],
        }
    )
    repo = CharacterizationRepository(mysql_adapter=None, mongodb_adapter=adapter)
    scheduler = DrawdownScheduler(
        mongodb_adapter=adapter,
        characterization_repository=repo,
        nats_client=AsyncMock(),
        strategy_ids=["ta-momentum", "grid-bot", "funding-arb"],
    )

    with caplog.at_level(logging.ERROR, logger=scheduler_module.__name__):
        results = await scheduler.run_cycle()

    assert [r.strategy_id for r in results] == [
        "ta-momentum",
        "grid-bot",
        "funding-arb",
    ]
    assert results[0].envelope == [5.0, 10.0, 20.0, 30.0]
    assert results[0].events_evaluated == 1
    assert not [
        r for r in caplog.records if "drawdown_cycle_strategy_failed" in r.message
    ]


# ---------------------------------------------------------------------------
# Log detail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strategy_failure_log_carries_strategy_exception_and_cause(caplog):
    adapter = _adapter()
    scheduler = DrawdownScheduler(
        mongodb_adapter=adapter,
        characterization_repository=AsyncMock(),
        nats_client=AsyncMock(),
        strategy_ids=["explodes"],
    )

    async def boom(strategy_id, *, start=None, end=None):
        try:
            raise ValueError("inner detail")
        except ValueError as inner:
            raise RuntimeError("outer failure") from inner

    scheduler._service.compute = boom  # type: ignore[assignment]

    with caplog.at_level(logging.ERROR, logger=scheduler_module.__name__):
        results = await scheduler.run_cycle()

    assert results == []
    (record,) = [
        r for r in caplog.records if "drawdown_cycle_strategy_failed" in r.message
    ]
    message = record.getMessage()
    assert "strategy_id=explodes" in message
    assert "error_type=RuntimeError" in message
    assert "error=outer failure" in message
    assert "cause=ValueError: inner detail" in message
    assert record.exc_info is not None
    assert record.strategy_id == "explodes"
    assert record.cause == "ValueError: inner detail"


def test_describe_cause_variants():
    assert _describe_cause(RuntimeError("x")) == "none"
    try:
        try:
            raise KeyError("k")
        except KeyError:
            raise RuntimeError("implicit")  # noqa: B904 — testing __context__
    except RuntimeError as exc:
        assert _describe_cause(exc) == "KeyError: 'k'"


@pytest.mark.asyncio
async def test_cycle_failure_log_includes_error(caplog, monkeypatch):
    scheduler = DrawdownScheduler(
        mongodb_adapter=_adapter(),
        characterization_repository=AsyncMock(),
        nats_client=AsyncMock(),
        strategy_ids=[],
    )

    async def failing_cycle():
        scheduler._running = False
        raise RuntimeError("cycle blew up")

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(scheduler, "run_cycle", failing_cycle)
    monkeypatch.setattr(scheduler_module.asyncio, "sleep", no_sleep)
    scheduler._running = True

    with caplog.at_level(logging.ERROR, logger=scheduler_module.__name__):
        await scheduler._loop()

    (record,) = [
        r for r in caplog.records if "drawdown_scheduler_cycle_failed" in r.message
    ]
    assert "error_type=RuntimeError error=cycle blew up" in record.getMessage()
    assert record.exc_info is not None


@pytest.mark.asyncio
async def test_discovery_failure_log_includes_error(caplog):
    adapter = _adapter()
    adapter.list_all_strategy_ids = AsyncMock(side_effect=ConnectionError("down"))
    scheduler = DrawdownScheduler(
        mongodb_adapter=adapter,
        characterization_repository=AsyncMock(),
        nats_client=AsyncMock(),
    )
    with caplog.at_level(logging.WARNING, logger=scheduler_module.__name__):
        assert await scheduler.run_cycle() == []
    (record,) = [
        r for r in caplog.records if "drawdown_scheduler_discovery_failed" in r.message
    ]
    assert "error_type=ConnectionError error=down" in record.getMessage()


@pytest.mark.asyncio
async def test_pnl_fetch_failure_log_includes_strategy_and_error(caplog):
    from data_manager.portfolio import drawdown_service
    from data_manager.portfolio.drawdown_service import DrawdownService

    adapter = MagicMock()
    adapter._connected = True
    adapter.db = MagicMock()
    adapter.db.__getitem__ = MagicMock(side_effect=RuntimeError("mongo gone"))
    service = DrawdownService(mongodb_adapter=adapter)
    with caplog.at_level(logging.ERROR, logger=drawdown_service.__name__):
        result = await service.compute("s1")
    assert result.events_evaluated == 0
    (record,) = [r for r in caplog.records if "drawdown_pnl_fetch_failed" in r.message]
    assert (
        "strategy_id=s1 error_type=RuntimeError error=mongo gone" in record.getMessage()
    )


# ---------------------------------------------------------------------------
# Sibling repositories with the same guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_characterization_repository_methods_with_motor_like_db():
    doc = _characterization_doc("s1", [1.0, 2.0, 3.0, 4.0])
    doc["strategy_revision_id"] = "rev-1"
    adapter = _adapter({"characterizations": [doc]})
    repo = CharacterizationRepository(mysql_adapter=None, mongodb_adapter=adapter)

    latest = await repo.get_latest("s1")
    assert latest is not None and latest.strategy_id == "s1"
    assert (await repo.get_version("s1", "v1")).strategy_version == "v1"
    assert (await repo.get_by_strategy_revision("s1", "rev-1")) is not None
    assert await repo.upsert(latest) is True


@pytest.mark.asyncio
async def test_characterization_repository_without_db_returns_none():
    adapter = MagicMock()
    adapter.db = None
    repo = CharacterizationRepository(mysql_adapter=None, mongodb_adapter=adapter)
    assert await repo.get_latest("s1") is None
    assert await repo.get_version("s1", "v1") is None
    assert await repo.get_by_strategy_revision("s1", "rev") is None
    no_adapter = CharacterizationRepository(mysql_adapter=None, mongodb_adapter=None)
    assert await no_adapter.get_latest("s1") is None


def test_strategy_lifecycle_repository_collection_with_motor_like_db():
    adapter = _adapter()
    repo = StrategyLifecycleRepository(mysql_adapter=None, mongodb_adapter=adapter)
    assert repo._collection() is not None
    assert adapter.db.accessed == ["strategy_lifecycle_events"]

    adapter.db = None
    with pytest.raises(RuntimeError, match="MongoDB is not available"):
        repo._collection()


@pytest.mark.asyncio
async def test_strategy_timeline_fetch_with_motor_like_db():
    rows = [{"strategy_id": "s1", "timestamp": NOW, "_id": "e1"}]
    adapter = _adapter({"strategy_lifecycle_events": rows})
    repo = StrategyTimelineRepository(mysql_adapter=None, mongodb_adapter=adapter)
    assert await repo._fetch("lifecycle", "s1", None, None, 10) == rows

    adapter.db = None
    assert await repo._fetch("lifecycle", "s1", None, None, 10) == []


@pytest.mark.asyncio
async def test_lifecycle_repository_with_motor_like_db():
    adapter = _adapter()
    adapter.find_filtered = AsyncMock(return_value=[])
    repo = LifecycleRepository(mysql_adapter=None, mongodb_adapter=adapter)

    # No decision row -> None, but the lookup must actually reach Mongo.
    assert await repo.reconstruct("d-1") is None
    assert "cio_decisions" in adapter.db.accessed
    assert await repo.reconstruct_by_strategy("s1") == []
    adapter.find_filtered.assert_awaited_once()

    adapter.db = None
    assert await repo.reconstruct("d-1") is None
    assert await repo.reconstruct_by_strategy("s1") == []
