"""Tests for the lifecycle reconstruction join (#603 P4.3).

Covers:
  * ``LifecycleRepository.reconstruct`` joins all four collections,
    sorts each leg chronologically, strips ``_id``, and returns ``None``
    when the anchor decision doesn't exist.
  * ``LifecycleRepository.reconstruct_by_strategy`` lists decisions,
    reconstructs each, and survives per-decision failures.
  * The summary fields (``has_filled``, ``realized_pnl_usd`` over
    closed events, counts) are derived correctly.
  * The HTTP route — 200 / 404 / 503 / 500 paths and the strategy list
    variant.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

try:
    from datetime import UTC
except ImportError:
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

from data_manager.db.repositories.lifecycle_repository import LifecycleRepository

NOW = datetime(2026, 5, 21, 12, 0, tzinfo=UTC)


class _FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *_args, **_kwargs):
        return self

    async def to_list(self, length=None):
        return self._docs


def _adapter_with_collections(mapping):
    """Build a stub MongoDB adapter where each collection name maps to
    a (find_one_result, find_many_results) tuple."""
    adapter = MagicMock()
    adapter._connected = True

    db = MagicMock()

    def get_coll(name):
        find_one_doc, find_many_docs = mapping.get(name, (None, []))
        coll = MagicMock()
        coll.find_one = AsyncMock(return_value=find_one_doc)
        coll.find = MagicMock(return_value=_FakeCursor(find_many_docs))
        return coll

    db.__getitem__ = MagicMock(side_effect=get_coll)
    adapter.db = db
    return adapter


# ---------------------------------------------------------------------------
# LifecycleRepository.reconstruct
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconstruct_returns_none_when_decision_missing():
    adapter = _adapter_with_collections({"cio_decisions": (None, [])})
    repo = LifecycleRepository(mysql_adapter=None, mongodb_adapter=adapter)

    result = await repo.reconstruct("d-missing")
    assert result is None


@pytest.mark.asyncio
async def test_reconstruct_joins_all_four_collections():
    decision = {
        "decision_id": "d-1",
        "strategy_id": "ta-momentum",
        "action": "execute",
        "timestamp": NOW,
        "_id": "internal",
    }
    intent = {
        "intent_id": "i-1",
        "decision_id": "d-1",
        "timestamp": NOW - timedelta(seconds=2),
        "_id": "x",
    }
    exec_placed = {
        "decision_id": "d-1",
        "order_id": "o-1",
        "event_type": "placed",
        "timestamp": NOW + timedelta(seconds=1),
        "_id": "y",
    }
    exec_filled = {
        "decision_id": "d-1",
        "order_id": "o-1",
        "event_type": "filled",
        "timestamp": NOW + timedelta(seconds=2),
        "_id": "z",
    }
    pnl_closed = {
        "decision_id": "d-1",
        "pnl_kind": "closed",
        "realized_pnl_usd": 12.5,
        "timestamp": NOW + timedelta(minutes=30),
        "_id": "w",
    }

    adapter = _adapter_with_collections(
        {
            "cio_decisions": (decision, []),
            "intents": (None, [intent]),
            "execution_events": (None, [exec_placed, exec_filled]),
            "pnl_events": (None, [pnl_closed]),
        }
    )
    repo = LifecycleRepository(mysql_adapter=None, mongodb_adapter=adapter)

    result = await repo.reconstruct("d-1")
    assert result is not None
    # _id is stripped from every leg.
    assert "_id" not in result["decision"]
    assert all("_id" not in i for i in result["intents"])
    assert all("_id" not in e for e in result["executions"])
    assert all("_id" not in p for p in result["pnl_events"])
    assert result["decision_id"] == "d-1"
    assert result["intents"][0]["intent_id"] == "i-1"
    assert [e["event_type"] for e in result["executions"]] == ["placed", "filled"]
    assert result["pnl_events"][0]["realized_pnl_usd"] == 12.5

    summary = result["summary"]
    assert summary["action"] == "execute"
    assert summary["strategy_id"] == "ta-momentum"
    assert summary["executions_count"] == 2
    assert summary["pnl_events_count"] == 1
    assert summary["has_filled"] is True
    assert summary["realized_pnl_usd"] == 12.5


@pytest.mark.asyncio
async def test_reconstruct_summary_handles_empty_legs():
    decision = {
        "decision_id": "d-2",
        "strategy_id": "ta-momentum",
        "action": "skip",
        "timestamp": NOW,
    }
    adapter = _adapter_with_collections(
        {
            "cio_decisions": (decision, []),
            "intents": (None, []),
            "execution_events": (None, []),
            "pnl_events": (None, []),
        }
    )
    repo = LifecycleRepository(mysql_adapter=None, mongodb_adapter=adapter)

    result = await repo.reconstruct("d-2")
    assert result is not None
    summary = result["summary"]
    assert summary["has_filled"] is False
    assert summary["executions_count"] == 0
    assert summary["pnl_events_count"] == 0
    # realized_pnl_usd is None when no closed events exist — operators
    # see a deliberate "not applicable" rather than a fake 0.0.
    assert summary["realized_pnl_usd"] is None


@pytest.mark.asyncio
async def test_reconstruct_summary_realized_pnl_excludes_non_closed():
    """``realized_pnl_usd`` should only sum events where
    ``pnl_kind == "closed"``. mark_to_market snapshots have unrealized
    P&L and must NOT be folded into realized."""
    decision = {
        "decision_id": "d-3",
        "strategy_id": "ta-momentum",
        "action": "execute",
        "timestamp": NOW,
    }
    pnl_events = [
        {
            "decision_id": "d-3",
            "pnl_kind": "mark_to_market",
            "unrealized_pnl_usd": 30.0,
            "timestamp": NOW,
        },
        {
            "decision_id": "d-3",
            "pnl_kind": "closed",
            "realized_pnl_usd": 25.0,
            "timestamp": NOW + timedelta(minutes=10),
        },
        {
            "decision_id": "d-3",
            "pnl_kind": "closed",
            "realized_pnl_usd": -5.0,
            "timestamp": NOW + timedelta(minutes=20),
        },
    ]
    adapter = _adapter_with_collections(
        {
            "cio_decisions": (decision, []),
            "intents": (None, []),
            "execution_events": (None, []),
            "pnl_events": (None, pnl_events),
        }
    )
    repo = LifecycleRepository(mysql_adapter=None, mongodb_adapter=adapter)

    result = await repo.reconstruct("d-3")
    assert result["summary"]["realized_pnl_usd"] == 20.0  # 25 - 5


@pytest.mark.asyncio
async def test_reconstruct_returns_none_when_mongo_unavailable():
    repo = LifecycleRepository(mysql_adapter=None, mongodb_adapter=None)
    assert await repo.reconstruct("d-1") is None


# ---------------------------------------------------------------------------
# reconstruct_by_strategy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconstruct_by_strategy_iterates_decisions():
    adapter = MagicMock()
    adapter._connected = True

    # find_filtered is the #605 helper — returns one row per decision.
    adapter.find_filtered = AsyncMock(
        return_value=[
            {
                "decision_id": "d-a",
                "strategy_id": "ta",
                "action": "execute",
                "timestamp": NOW,
            },
            {
                "decision_id": "d-b",
                "strategy_id": "ta",
                "action": "skip",
                "timestamp": NOW - timedelta(minutes=10),
            },
        ]
    )

    # Build a per-decision-id mock for db[<collection>] reads.
    leg_doc = lambda did: {  # noqa: E731
        "decision_id": did,
        "timestamp": NOW,
    }
    decision_doc = lambda did: {  # noqa: E731
        "decision_id": did,
        "strategy_id": "ta",
        "action": "execute",
        "timestamp": NOW,
    }

    db = MagicMock()

    def get_coll(name):
        coll = MagicMock()
        if name == "cio_decisions":
            coll.find_one = AsyncMock(
                side_effect=lambda q: decision_doc(q["decision_id"])
            )
        else:
            coll.find_one = AsyncMock(return_value=None)
        coll.find = MagicMock(return_value=_FakeCursor([leg_doc(name + "-event")]))
        return coll

    db.__getitem__ = MagicMock(side_effect=get_coll)
    adapter.db = db

    repo = LifecycleRepository(mysql_adapter=None, mongodb_adapter=adapter)
    results = await repo.reconstruct_by_strategy("ta", limit=5)
    assert len(results) == 2
    assert {r["decision_id"] for r in results} == {"d-a", "d-b"}
    adapter.find_filtered.assert_awaited_once()
    kwargs = adapter.find_filtered.call_args.kwargs
    assert kwargs["filters"] == {"strategy_id": "ta"}


@pytest.mark.asyncio
async def test_reconstruct_by_strategy_returns_empty_when_no_mongo():
    repo = LifecycleRepository(mysql_adapter=None, mongodb_adapter=None)
    assert await repo.reconstruct_by_strategy("ta") == []


@pytest.mark.asyncio
async def test_reconstruct_by_strategy_skips_per_decision_failures():
    adapter = MagicMock()
    adapter._connected = True
    adapter.find_filtered = AsyncMock(
        return_value=[
            {"decision_id": "ok", "strategy_id": "ta", "timestamp": NOW},
            {"decision_id": "boom", "strategy_id": "ta", "timestamp": NOW},
        ]
    )

    db = MagicMock()

    def get_coll(name):
        coll = MagicMock()
        if name == "cio_decisions":

            async def find_one(q):
                if q["decision_id"] == "boom":
                    raise RuntimeError("synthetic")
                return {
                    "decision_id": q["decision_id"],
                    "strategy_id": "ta",
                    "timestamp": NOW,
                }

            coll.find_one = AsyncMock(side_effect=find_one)
        else:
            coll.find_one = AsyncMock(return_value=None)
        coll.find = MagicMock(return_value=_FakeCursor([]))
        return coll

    db.__getitem__ = MagicMock(side_effect=get_coll)
    adapter.db = db

    repo = LifecycleRepository(mysql_adapter=None, mongodb_adapter=adapter)
    results = await repo.reconstruct_by_strategy("ta")
    # "boom" decision's find_one raised → _fetch_one swallows the
    # error and returns None → reconstruct() returns None → skipped.
    # "ok" decision still reconstructs.
    assert [r["decision_id"] for r in results] == ["ok"]


# ---------------------------------------------------------------------------
# HTTP route — GET /api/v1/lifecycle/{decision_id}
# ---------------------------------------------------------------------------


def _client_with_db(db_manager):
    from fastapi.testclient import TestClient

    import data_manager.api.app as api_module
    from data_manager.api.app import create_app

    api_module.db_manager = db_manager
    return TestClient(create_app())


def test_single_route_503_when_db_missing():
    client = _client_with_db(None)
    r = client.get("/api/v1/lifecycle/d-1")
    assert r.status_code == 503


def test_single_route_404_when_decision_missing():
    db = MagicMock()
    db.mongodb_adapter = MagicMock()
    db.mysql_adapter = MagicMock()
    with patch(
        "data_manager.db.repositories.lifecycle_repository.LifecycleRepository.reconstruct",
        new=AsyncMock(return_value=None),
    ):
        client = _client_with_db(db)
        r = client.get("/api/v1/lifecycle/d-missing")
    assert r.status_code == 404
    assert "decision_id=d-missing" in r.json()["detail"]


def test_single_route_returns_reconstruction():
    db = MagicMock()
    db.mongodb_adapter = MagicMock()
    db.mysql_adapter = MagicMock()
    fake = {
        "decision_id": "d-1",
        "decision": {"decision_id": "d-1", "action": "execute"},
        "intents": [],
        "executions": [],
        "pnl_events": [],
        "summary": {"action": "execute", "executions_count": 0},
    }
    with patch(
        "data_manager.db.repositories.lifecycle_repository.LifecycleRepository.reconstruct",
        new=AsyncMock(return_value=fake),
    ):
        client = _client_with_db(db)
        r = client.get("/api/v1/lifecycle/d-1")
    assert r.status_code == 200
    assert r.json()["decision_id"] == "d-1"


def test_single_route_500_when_repo_raises():
    db = MagicMock()
    db.mongodb_adapter = MagicMock()
    db.mysql_adapter = MagicMock()
    with patch(
        "data_manager.db.repositories.lifecycle_repository.LifecycleRepository.reconstruct",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        client = _client_with_db(db)
        r = client.get("/api/v1/lifecycle/d-1")
    assert r.status_code == 500


# ---------------------------------------------------------------------------
# HTTP route — GET /api/v1/lifecycle?strategy_id=...
# ---------------------------------------------------------------------------


def test_strategy_route_requires_strategy_id():
    db = MagicMock()
    db.mongodb_adapter = MagicMock()
    db.mysql_adapter = MagicMock()
    client = _client_with_db(db)
    r = client.get("/api/v1/lifecycle")
    assert r.status_code == 422


def test_strategy_route_returns_list_and_threads_filters():
    db = MagicMock()
    db.mongodb_adapter = MagicMock()
    db.mysql_adapter = MagicMock()
    fake_list = [
        {
            "decision_id": "d-1",
            "decision": {},
            "intents": [],
            "executions": [],
            "pnl_events": [],
            "summary": {},
        },
        {
            "decision_id": "d-2",
            "decision": {},
            "intents": [],
            "executions": [],
            "pnl_events": [],
            "summary": {},
        },
    ]
    with patch(
        "data_manager.db.repositories.lifecycle_repository.LifecycleRepository.reconstruct_by_strategy",
        new=AsyncMock(return_value=fake_list),
    ) as mock_recon:
        client = _client_with_db(db)
        r = client.get(
            "/api/v1/lifecycle",
            params={
                "strategy_id": "ta-momentum",
                "from": "2026-05-21T11:00:00+00:00",
                "to": "2026-05-21T13:00:00+00:00",
                "limit": 10,
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert body["strategy_id"] == "ta-momentum"
    assert body["count"] == 2
    assert body["limit"] == 10
    assert body["from"] == "2026-05-21T11:00:00+00:00"
    kwargs = mock_recon.await_args.kwargs
    assert kwargs["start"] == datetime(2026, 5, 21, 11, 0, tzinfo=UTC)
    assert kwargs["end"] == datetime(2026, 5, 21, 13, 0, tzinfo=UTC)
    assert kwargs["limit"] == 10


def test_strategy_route_limit_clamping():
    db = MagicMock()
    db.mongodb_adapter = MagicMock()
    db.mysql_adapter = MagicMock()
    client = _client_with_db(db)
    # Below min.
    assert (
        client.get(
            "/api/v1/lifecycle", params={"strategy_id": "ta", "limit": 0}
        ).status_code
        == 422
    )
    # Above max (200).
    assert (
        client.get(
            "/api/v1/lifecycle", params={"strategy_id": "ta", "limit": 9999}
        ).status_code
        == 422
    )
