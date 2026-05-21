"""Tests for the #605 P4.5 audit-trail decision-filter surface.

Covers:
  * ``AuditRepository.query_decisions`` composes its filter arg correctly,
    drops None values, threads time-window into ``find_filtered``, and
    survives adapter failure with an empty list.
  * ``MongoDBAdapter.find_filtered`` builds the right Mongo query for
    each filter-combination + time-window shape and strips ``_id``.
  * The ``GET /api/v1/audit/decisions`` route returns the AuditRepository
    output, surfaces 503 when the adapter is missing, and respects the
    filter query-params.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

try:
    from datetime import UTC
except ImportError:
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

from data_manager.db.repositories.audit_repository import AuditRepository

# ---------------------------------------------------------------------------
# AuditRepository.query_decisions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_decisions_threads_all_filters_through():
    """All optional filters propagate into find_filtered exactly once."""
    mongo = AsyncMock()
    mongo.find_filtered = AsyncMock(
        return_value=[{"decision_id": "d-1", "strategy_id": "ta", "action": "execute"}]
    )
    repo = AuditRepository(mysql_adapter=None, mongodb_adapter=mongo)
    start = datetime(2026, 5, 21, 12, 0, tzinfo=UTC)
    end = datetime(2026, 5, 21, 13, 0, tzinfo=UTC)

    out = await repo.query_decisions(
        strategy_id="ta-momentum",
        action="execute",
        decision_id="d-42",
        symbol="BTCUSDT",
        start=start,
        end=end,
        limit=25,
    )

    assert out[0]["decision_id"] == "d-1"
    mongo.find_filtered.assert_awaited_once()
    kwargs = mongo.find_filtered.call_args.kwargs
    assert kwargs["filters"] == {
        "strategy_id": "ta-momentum",
        "action": "execute",
        "decision_id": "d-42",
        "symbol": "BTCUSDT",
    }
    assert kwargs["start"] == start
    assert kwargs["end"] == end
    assert kwargs["limit"] == 25
    assert kwargs["sort_field"] == "timestamp"
    assert kwargs["sort_order"] == -1


@pytest.mark.asyncio
async def test_query_decisions_with_only_strategy_id_filter():
    """The minimum-filter case still threads correctly and doesn't
    invent ``action`` / ``decision_id`` filters when callers omit them.
    The adapter is responsible for skipping None values; the repo just
    forwards them."""
    mongo = AsyncMock()
    mongo.find_filtered = AsyncMock(return_value=[])
    repo = AuditRepository(mysql_adapter=None, mongodb_adapter=mongo)

    await repo.query_decisions(strategy_id="ta-momentum")

    kwargs = mongo.find_filtered.call_args.kwargs
    assert kwargs["filters"]["strategy_id"] == "ta-momentum"
    assert kwargs["filters"]["action"] is None
    assert kwargs["filters"]["decision_id"] is None
    assert kwargs["start"] is None
    assert kwargs["end"] is None


@pytest.mark.asyncio
async def test_query_decisions_returns_empty_when_no_mongo_adapter():
    """Repo must not crash when constructed without a MongoDB adapter —
    that scenario shows up in tests and in degraded deployments where
    Mongo is unreachable but MySQL still serves anomaly / gap audits."""
    repo = AuditRepository(mysql_adapter=Mock(), mongodb_adapter=None)
    assert await repo.query_decisions(strategy_id="ta") == []


@pytest.mark.asyncio
async def test_query_decisions_swallows_adapter_errors():
    """A Mongo error must surface as an empty list so the HTTP layer
    can serve 200 with an empty body — caller logs already capture the
    underlying failure."""
    mongo = AsyncMock()
    mongo.find_filtered = AsyncMock(side_effect=RuntimeError("mongo down"))
    repo = AuditRepository(mysql_adapter=None, mongodb_adapter=mongo)
    assert await repo.query_decisions(strategy_id="ta") == []


# ---------------------------------------------------------------------------
# MongoDBAdapter.find_filtered
# ---------------------------------------------------------------------------


class _FakeCursor:
    """Minimal stand-in for the Motor cursor chain used by find_filtered."""

    def __init__(self, docs):
        self._docs = docs

    def sort(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    async def to_list(self, length=None):
        return self._docs


def _adapter_with_collection(docs):
    """Build a connected MongoDBAdapter with a stubbed collection whose
    ``find()`` returns a fake cursor."""
    from data_manager.db.mongodb_adapter import MongoDBAdapter

    adapter = MongoDBAdapter.__new__(MongoDBAdapter)
    adapter._connected = True
    coll = MagicMock()
    coll.find = MagicMock(return_value=_FakeCursor(docs))
    db = MagicMock()
    db.__getitem__ = MagicMock(return_value=coll)
    adapter.db = db
    return adapter, coll


@pytest.mark.asyncio
async def test_find_filtered_skips_none_filter_values():
    """``None`` filters must not bleed into the Mongo query as literal
    ``None`` comparisons (would never match)."""
    adapter, coll = _adapter_with_collection([])
    await adapter.find_filtered(
        "cio_decisions",
        filters={"strategy_id": "ta", "action": None, "decision_id": "d-1"},
        limit=10,
    )
    coll.find.assert_called_once()
    query = coll.find.call_args.args[0]
    assert query == {"strategy_id": "ta", "decision_id": "d-1"}


@pytest.mark.asyncio
async def test_find_filtered_composes_time_window_with_filters():
    adapter, coll = _adapter_with_collection([])
    start = datetime(2026, 5, 21, 12, 0, tzinfo=UTC)
    end = datetime(2026, 5, 21, 13, 0, tzinfo=UTC)
    await adapter.find_filtered(
        "cio_decisions",
        filters={"strategy_id": "ta"},
        start=start,
        end=end,
        limit=10,
    )
    query = coll.find.call_args.args[0]
    assert query["strategy_id"] == "ta"
    assert query["timestamp"] == {"$gte": start, "$lt": end}


@pytest.mark.asyncio
async def test_find_filtered_strips_mongo_id_from_results():
    adapter, _ = _adapter_with_collection(
        [{"_id": "abc", "decision_id": "d-1"}, {"_id": "def", "decision_id": "d-2"}]
    )
    out = await adapter.find_filtered("cio_decisions", filters={}, limit=10)
    assert all("_id" not in doc for doc in out)
    assert [d["decision_id"] for d in out] == ["d-1", "d-2"]


@pytest.mark.asyncio
async def test_find_filtered_raises_when_not_connected():
    from data_manager.db.base_adapter import DatabaseError
    from data_manager.db.mongodb_adapter import MongoDBAdapter

    adapter = MongoDBAdapter.__new__(MongoDBAdapter)
    adapter._connected = False
    with pytest.raises(DatabaseError):
        await adapter.find_filtered("cio_decisions", filters={}, limit=10)


# ---------------------------------------------------------------------------
# HTTP route — GET /api/v1/audit/decisions
# ---------------------------------------------------------------------------


def _client_with_db(db_manager):
    """Build a TestClient with the global api_module.db_manager patched."""
    from fastapi.testclient import TestClient

    import data_manager.api.app as api_module
    from data_manager.api.app import create_app

    api_module.db_manager = db_manager
    return TestClient(create_app())


def test_route_returns_503_when_db_manager_missing():
    client = _client_with_db(None)
    r = client.get("/api/v1/audit/decisions")
    assert r.status_code == 503


def test_route_returns_503_when_mongo_missing():
    db = MagicMock()
    db.mongodb_adapter = None
    client = _client_with_db(db)
    r = client.get("/api/v1/audit/decisions")
    assert r.status_code == 503


def test_route_returns_decisions_with_filters_threaded_through():
    db = MagicMock()
    db.mysql_adapter = MagicMock()
    db.mongodb_adapter = MagicMock()

    with patch(
        "data_manager.db.repositories.audit_repository.AuditRepository.query_decisions",
        new=AsyncMock(
            return_value=[
                {
                    "decision_id": "d-1",
                    "strategy_id": "ta-momentum",
                    "action": "execute",
                    "timestamp": "2026-05-21T12:00:00+00:00",
                }
            ]
        ),
    ) as mock_query:
        client = _client_with_db(db)
        r = client.get(
            "/api/v1/audit/decisions",
            params={
                "strategy_id": "ta-momentum",
                "action": "execute",
                "limit": 50,
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["filters"]["strategy_id"] == "ta-momentum"
    assert body["filters"]["action"] == "execute"
    assert body["limit"] == 50
    assert body["decisions"][0]["decision_id"] == "d-1"
    mock_query.assert_awaited_once()
    call_kwargs = mock_query.await_args.kwargs
    assert call_kwargs["strategy_id"] == "ta-momentum"
    assert call_kwargs["action"] == "execute"
    assert call_kwargs["limit"] == 50


def test_route_clamps_limit_to_allowed_range():
    db = MagicMock()
    db.mysql_adapter = MagicMock()
    db.mongodb_adapter = MagicMock()
    client = _client_with_db(db)

    # 0 is below the minimum (ge=1) → 422.
    r = client.get("/api/v1/audit/decisions", params={"limit": 0})
    assert r.status_code == 422

    # 10000 is above the cap (le=1000) → 422.
    r = client.get("/api/v1/audit/decisions", params={"limit": 10000})
    assert r.status_code == 422


def test_route_returns_500_when_query_raises():
    db = MagicMock()
    db.mysql_adapter = MagicMock()
    db.mongodb_adapter = MagicMock()
    with patch(
        "data_manager.db.repositories.audit_repository.AuditRepository.query_decisions",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        client = _client_with_db(db)
        r = client.get("/api/v1/audit/decisions")
    assert r.status_code == 500


def test_route_accepts_time_window_via_from_to_aliases():
    db = MagicMock()
    db.mysql_adapter = MagicMock()
    db.mongodb_adapter = MagicMock()

    with patch(
        "data_manager.db.repositories.audit_repository.AuditRepository.query_decisions",
        new=AsyncMock(return_value=[]),
    ) as mock_query:
        client = _client_with_db(db)
        r = client.get(
            "/api/v1/audit/decisions",
            params={
                "from": "2026-05-21T12:00:00+00:00",
                "to": "2026-05-21T13:00:00+00:00",
            },
        )
    assert r.status_code == 200
    kwargs = mock_query.await_args.kwargs
    assert kwargs["start"] == datetime(2026, 5, 21, 12, 0, tzinfo=UTC)
    assert kwargs["end"] == datetime(2026, 5, 21, 13, 0, tzinfo=UTC)
