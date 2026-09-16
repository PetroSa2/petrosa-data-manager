"""
Regression tests for petrosa-data-manager#282.

Generic query API (GET /api/v1/{database}/{collection} and legacy POST
/api/v1/data/query) previously loaded the ENTIRE collection/table into memory
via query_range(start=datetime.min, end=datetime.max) and applied filter,
sort, and pagination in Python — response time scaled with collection size
instead of `limit`, making `klines_5m` (68k+ docs) effectively unqueryable.

These tests cover:
  1. MongoDBAdapter.find_paginated / MySQLAdapter.find_paginated push
     filter/sort/limit/offset to the driver and source `total` from a count
     query, not `len(records)`.
  2. Operator-injection is refused (flat equality filter only) on both
     adapters — the unauthenticated `filter` query param must not accept raw
     Mongo operators or SQL-adjacent tricks.
  3. `_execute_query_internal` (the shared helper behind both the generic GET
     route and the legacy POST query route) calls `find_paginated` with the
     driver receiving the actual `limit`/`offset` — NOT a post-hoc Python
     slice of a full collection dump (i.e. `query_range` is never called from
     this path anymore).
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

import data_manager.api.app as api_module
from data_manager.db.base_adapter import DatabaseError
from data_manager.db.mongodb_adapter import MongoDBAdapter
from data_manager.db.mysql_adapter import MySQLAdapter

# ---------------------------------------------------------------------------
# MongoDBAdapter.find_paginated
# ---------------------------------------------------------------------------


@pytest.fixture
def mongo_adapter():
    a = MongoDBAdapter("mongodb://localhost:27017/test_db")
    a.client = MagicMock()
    a.db = MagicMock()
    a._connected = True
    return a


class TestMongoFindPaginated:
    @pytest.mark.asyncio
    async def test_raises_when_not_connected(self, mongo_adapter):
        mongo_adapter._connected = False
        with pytest.raises(DatabaseError, match="Not connected") as exc_info:
            await mongo_adapter.find_paginated("klines_5m")
        assert "Not connected" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_pushes_filter_sort_limit_offset_to_driver(self, mongo_adapter):
        """The driver — not Python — must receive limit/offset/sort."""
        cursor = MagicMock()
        cursor.sort.return_value = cursor
        cursor.skip.return_value = cursor
        cursor.limit.return_value = cursor
        cursor.to_list = AsyncMock(
            return_value=[{"_id": "x", "symbol": "BTCUSDT", "close": 1}]
        )
        coll = MagicMock()
        coll.find.return_value = cursor
        coll.count_documents = AsyncMock(return_value=68330)
        mongo_adapter.db.__getitem__ = MagicMock(return_value=coll)

        documents, total = await mongo_adapter.find_paginated(
            "klines_5m",
            filter_dict={"symbol": "BTCUSDT"},
            sort_list=[("timestamp", -1)],
            limit=1,
            offset=0,
        )

        assert total == 68330
        assert len(documents) == 1
        assert "_id" not in documents[0]

        coll.find.assert_called_once_with({"symbol": "BTCUSDT"})
        cursor.sort.assert_called_once_with([("timestamp", -1)])
        cursor.skip.assert_called_once_with(0)
        cursor.limit.assert_called_once_with(1)
        coll.count_documents.assert_awaited_once_with({"symbol": "BTCUSDT"})

    @pytest.mark.asyncio
    async def test_total_reflects_full_filtered_set_not_page_size(self, mongo_adapter):
        """total must come from count_documents(), independent of `limit`."""
        cursor = MagicMock()
        cursor.sort.return_value = cursor
        cursor.skip.return_value = cursor
        cursor.limit.return_value = cursor
        cursor.to_list = AsyncMock(return_value=[{"v": 1}])
        coll = MagicMock()
        coll.find.return_value = cursor
        coll.count_documents = AsyncMock(return_value=629)
        mongo_adapter.db.__getitem__ = MagicMock(return_value=coll)

        documents, total = await mongo_adapter.find_paginated("klines_1d", limit=1)
        assert len(documents) == 1
        assert total == 629

    @pytest.mark.asyncio
    async def test_offset_pushed_to_skip(self, mongo_adapter):
        cursor = MagicMock()
        cursor.sort.return_value = cursor
        cursor.skip.return_value = cursor
        cursor.limit.return_value = cursor
        cursor.to_list = AsyncMock(return_value=[])
        coll = MagicMock()
        coll.find.return_value = cursor
        coll.count_documents = AsyncMock(return_value=0)
        mongo_adapter.db.__getitem__ = MagicMock(return_value=coll)

        await mongo_adapter.find_paginated("x", limit=50, offset=100)
        cursor.skip.assert_called_once_with(100)
        cursor.limit.assert_called_once_with(50)

    @pytest.mark.asyncio
    async def test_no_sort_list_skips_sort_call(self, mongo_adapter):
        cursor = MagicMock()
        cursor.skip.return_value = cursor
        cursor.limit.return_value = cursor
        cursor.to_list = AsyncMock(return_value=[])
        coll = MagicMock()
        coll.find.return_value = cursor
        coll.count_documents = AsyncMock(return_value=0)
        mongo_adapter.db.__getitem__ = MagicMock(return_value=coll)

        await mongo_adapter.find_paginated("x")
        cursor.sort.assert_not_called()

    @pytest.mark.parametrize(
        "bad_filter",
        [
            {"$where": "this.symbol == 'BTCUSDT'"},
            {"timestamp": {"$gte": 5}},
        ],
    )
    @pytest.mark.asyncio
    async def test_refuses_operator_injection(self, mongo_adapter, bad_filter):
        with pytest.raises(DatabaseError, match="flat equality match") as exc_info:
            await mongo_adapter.find_paginated("x", filter_dict=bad_filter)
        assert "flat equality match" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_pymongo_error_wrapped(self, mongo_adapter):
        from pymongo.errors import PyMongoError

        coll = MagicMock()
        coll.count_documents = AsyncMock(side_effect=PyMongoError("boom"))
        mongo_adapter.db.__getitem__ = MagicMock(return_value=coll)
        with pytest.raises(DatabaseError, match="Failed to query"):
            await mongo_adapter.find_paginated("x")


# ---------------------------------------------------------------------------
# MySQLAdapter.find_paginated (real SQLite engine, like test_mysql_adapter_methods.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_adapter():
    a = MySQLAdapter("sqlite:///:memory:")
    a.engine_options = {}
    a.engine = sa.create_engine("sqlite:///:memory:")
    a._connected = True
    a._create_tables()
    return a


def _dataset_row(i: int, category: str) -> dict:
    return {
        "dataset_id": f"d{i}",
        "name": f"dataset-{i}",
        "category": category,
        "storage_type": "mysql",
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=i),
    }


def _seed_datasets(adapter, rows):
    table = adapter._get_table("datasets")
    with adapter.engine.connect() as conn:
        conn.execute(sa.insert(table), rows)
        conn.commit()


class TestMySQLFindPaginated:
    def test_raises_when_not_connected(self, sqlite_adapter):
        sqlite_adapter._connected = False
        with pytest.raises(DatabaseError, match="Not connected") as exc_info:
            sqlite_adapter.find_paginated("datasets")
        assert "Not connected" in str(exc_info.value)

    def test_filter_sort_limit_offset_against_real_rows(self, sqlite_adapter):
        _seed_datasets(
            sqlite_adapter,
            [
                _dataset_row(i, "BTCUSDT" if i % 2 == 0 else "ETHUSDT")
                for i in range(10)
            ],
        )

        records, total = sqlite_adapter.find_paginated(
            "datasets",
            filter_dict={"category": "BTCUSDT"},
            sort_list=[("updated_at", -1)],
            limit=2,
            offset=1,
        )

        # 5 rows match category=BTCUSDT (even i: 0,2,4,6,8) regardless of limit.
        assert total == 5
        assert len(records) == 2
        # Sorted descending by updated_at (monotonic with i): skip d8, take d6, d4.
        assert [r["dataset_id"] for r in records] == ["d6", "d4"]

    def test_unknown_filter_column_returns_empty_not_error(self, sqlite_adapter):
        _seed_datasets(sqlite_adapter, [_dataset_row(1, "BTCUSDT")])
        records, total = sqlite_adapter.find_paginated(
            "datasets", filter_dict={"no_such_column": "x"}
        )
        assert records == []
        assert total == 0

    def test_unknown_sort_column_is_skipped_not_error(self, sqlite_adapter):
        _seed_datasets(sqlite_adapter, [_dataset_row(1, "BTCUSDT")])
        records, total = sqlite_adapter.find_paginated(
            "datasets", sort_list=[("no_such_column", -1)]
        )
        assert total == 1
        assert len(records) == 1

    def test_total_independent_of_limit(self, sqlite_adapter):
        _seed_datasets(sqlite_adapter, [_dataset_row(i, "BTCUSDT") for i in range(20)])
        records, total = sqlite_adapter.find_paginated("datasets", limit=1)
        assert len(records) == 1
        assert total == 20

    @pytest.mark.parametrize(
        "bad_filter",
        [
            {"$where": "1=1"},
            {"name": {"$eq": "BTCUSDT"}},
        ],
    )
    def test_refuses_operator_injection(self, sqlite_adapter, bad_filter):
        with pytest.raises(DatabaseError, match="flat equality match") as exc_info:
            sqlite_adapter.find_paginated("datasets", filter_dict=bad_filter)
        assert "flat equality match" in str(exc_info.value)


# ---------------------------------------------------------------------------
# generic.py::_execute_query_internal — driver receives the real limit,
# query_range is never invoked from this path (AC: "Regression test
# asserting the driver receives the limit, not a post-hoc slice").
# ---------------------------------------------------------------------------


@pytest.fixture
def client(mock_db_manager):
    mock_db_manager.mongodb_adapter = Mock()
    mock_db_manager.mongodb_adapter.query_range = AsyncMock(
        side_effect=AssertionError(
            "query_range must not be called from the generic query path (#282)"
        )
    )
    mock_db_manager.mongodb_adapter.find_paginated = AsyncMock(
        return_value=([{"symbol": "BTCUSDT"}], 68330)
    )

    mock_db_manager.mysql_adapter = Mock()
    mock_db_manager.mysql_adapter.query_range = Mock(
        side_effect=AssertionError(
            "query_range must not be called from the generic query path (#282)"
        )
    )
    mock_db_manager.mysql_adapter.find_paginated = Mock(return_value=([], 0))

    app = api_module.create_app()
    api_module.db_manager = mock_db_manager
    yield TestClient(app)
    api_module.db_manager = None


class TestGenericQueryDriverPushdown:
    def test_get_records_pushes_limit_to_driver(self, client):
        response = client.get("/api/v1/mongodb/klines_5m", params={"limit": 1})
        assert response.status_code == 200
        body = response.json()
        assert body["pagination"]["limit"] == 1
        assert body["pagination"]["total"] == 68330  # from count, not len(page)

        call = api_module.db_manager.mongodb_adapter.find_paginated.call_args
        assert call.kwargs["limit"] == 1
        assert call.kwargs["offset"] == 0

    def test_get_records_pushes_filter_and_sort(self, client):
        response = client.get(
            "/api/v1/mongodb/klines_5m",
            params={
                "filter": '{"symbol": "BTCUSDT"}',
                "sort": '{"timestamp": -1}',
                "limit": 5,
                "offset": 10,
            },
        )
        assert response.status_code == 200
        call = api_module.db_manager.mongodb_adapter.find_paginated.call_args
        assert call.kwargs["filter_dict"] == {"symbol": "BTCUSDT"}
        assert call.kwargs["sort_list"] == [("timestamp", -1)]
        assert call.kwargs["limit"] == 5
        assert call.kwargs["offset"] == 10

    def test_legacy_query_endpoint_pushes_limit_to_driver(self, client):
        payload = {
            "database": "mysql",
            "collection": "klines_1d",
            "filter": {"symbol": "BTCUSDT"},
            "limit": 1,
        }
        response = client.post("/api/v1/data/query", json=payload)
        assert response.status_code == 200
        call = api_module.db_manager.mysql_adapter.find_paginated.call_args
        assert call.kwargs["limit"] == 1
        assert call.kwargs["filter_dict"] == {"symbol": "BTCUSDT"}

    def test_database_error_surfaces_as_400(self, client):
        api_module.db_manager.mongodb_adapter.find_paginated = AsyncMock(
            side_effect=DatabaseError("filter must be a flat equality match, got x")
        )
        response = client.get(
            "/api/v1/mongodb/klines_5m",
            params={"filter": '{"$where": "1"}'},
        )
        assert response.status_code == 400
