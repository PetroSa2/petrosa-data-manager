"""DELETE /api/v1/{database}/{collection} really deletes (data-manager#378, G3).

It used to count in-memory matches and delete nothing.
"""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from pymongo.errors import PyMongoError

import data_manager.api.app as api_module
from data_manager.db.base_adapter import DatabaseError
from data_manager.db.mongodb_adapter import MongoDBAdapter
from data_manager.db.mysql_adapter import MySQLAdapter


@pytest.fixture
def client(mock_db_manager):
    mock_db_manager.mongodb_adapter.delete_many = AsyncMock(return_value=3)
    mock_db_manager.mysql_adapter.delete = Mock(return_value=2)
    api_module.db_manager = mock_db_manager
    yield TestClient(api_module.create_app())
    api_module.db_manager = None


def _delete(client, database, filter_dict, collection="positions"):
    return client.request(
        "DELETE", f"/api/v1/{database}/{collection}", json={"filter": filter_dict}
    )


class TestDeleteRoute:
    def test_mongodb_delete_calls_delete_many_and_returns_its_count(self, client):
        mongo = api_module.db_manager.mongodb_adapter

        response = _delete(client, "mongodb", {"position_id": "p1"})

        assert response.status_code == 200
        assert response.json()["deleted_count"] == 3
        mongo.delete_many.assert_awaited_once_with("positions", {"position_id": "p1"})
        mongo.query_range.assert_not_called()

    def test_mysql_delete_runs_off_the_event_loop_and_returns_its_count(self, client):
        mysql = api_module.db_manager.mysql_adapter
        real_to_thread = asyncio.to_thread

        async def passthrough(func, /, *args, **kwargs):
            return await real_to_thread(func, *args, **kwargs)

        with patch(
            "data_manager.api.routes.generic.asyncio.to_thread",
            side_effect=passthrough,
        ) as to_thread:
            response = _delete(client, "mysql", {"position_id": "p1"})

        assert response.status_code == 200
        assert response.json()["deleted_count"] == 2
        to_thread.assert_awaited_once_with(
            mysql.delete, "positions", {"position_id": "p1"}
        )
        mysql.query_range.assert_not_called()

    @pytest.mark.parametrize("database", ["mongodb", "mysql"])
    def test_empty_filter_is_400_and_deletes_nothing(self, client, database):
        response = _delete(client, database, {})

        assert response.status_code == 400
        assert "cannot be empty" in response.json()["detail"]
        api_module.db_manager.mongodb_adapter.delete_many.assert_not_called()
        api_module.db_manager.mysql_adapter.delete.assert_not_called()

    @pytest.mark.parametrize(
        "filter_dict", [{"$where": "true"}, {"position_id": {"$ne": "p1"}}]
    )
    def test_operator_filter_is_400(self, client, filter_dict):
        response = _delete(client, "mongodb", filter_dict)

        assert response.status_code == 400
        api_module.db_manager.mongodb_adapter.delete_many.assert_not_called()

    def test_unknown_database_is_400(self, client):
        response = _delete(client, "postgres", {"position_id": "p1"})

        assert response.status_code == 400

    def test_missing_adapter_is_503(self, client):
        api_module.db_manager.mysql_adapter = None

        response = _delete(client, "mysql", {"position_id": "p1"})

        assert response.status_code == 503

    def test_adapter_failure_is_500_and_counted(self, client):
        api_module.db_manager.mongodb_adapter.delete_many.side_effect = DatabaseError(
            "boom"
        )

        response = _delete(client, "mongodb", {"position_id": "p1"})

        assert response.status_code == 500
        api_module.db_manager.increment_error_count.assert_called_with("mongodb")


@pytest.fixture
def mongo_adapter():
    adapter = MongoDBAdapter("mongodb://localhost:27017/test_db")
    adapter.client = MagicMock()
    adapter.db = MagicMock()
    adapter._connected = True
    coll = MagicMock()
    coll.delete_many = AsyncMock(return_value=Mock(deleted_count=4))
    adapter.db.__getitem__ = MagicMock(return_value=coll)
    return adapter


class TestMongoDeleteMany:
    @pytest.mark.asyncio
    async def test_deletes_with_the_equality_filter(self, mongo_adapter):
        count = await mongo_adapter.delete_many("positions", {"position_id": "p1"})

        assert count == 4
        coll = mongo_adapter.db.__getitem__.return_value
        coll.delete_many.assert_awaited_once_with({"position_id": "p1"})

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("filter_dict", "match"),
        [({}, "empty filter"), ({"status": {"$in": ["a"]}}, "flat equality")],
    )
    async def test_never_deletes_with_unsafe_filter(
        self, mongo_adapter, filter_dict, match
    ):
        with pytest.raises(DatabaseError, match=match):
            await mongo_adapter.delete_many("positions", filter_dict)
        coll = mongo_adapter.db.__getitem__.return_value
        coll.delete_many.assert_not_called()

    @pytest.mark.asyncio
    async def test_driver_error_is_wrapped(self, mongo_adapter):
        coll = mongo_adapter.db.__getitem__.return_value
        coll.delete_many.side_effect = PyMongoError("down")

        with pytest.raises(DatabaseError, match="Failed to delete"):
            await mongo_adapter.delete_many("positions", {"position_id": "p1"})

    @pytest.mark.asyncio
    async def test_refuses_when_not_connected(self, mongo_adapter):
        mongo_adapter._connected = False

        with pytest.raises(DatabaseError, match="Not connected"):
            await mongo_adapter.delete_many("positions", {"position_id": "p1"})


@pytest.fixture
def sqlite_adapter():
    """MySQLAdapter on a real in-memory SQLite engine (see test_282)."""
    adapter = MySQLAdapter("sqlite:///:memory:")
    adapter.engine_options = {}
    adapter.engine = sa.create_engine("sqlite:///:memory:")
    adapter._connected = True
    adapter._create_tables()
    table = adapter._get_table("datasets")
    rows = [
        {
            "dataset_id": f"d{i}",
            "name": f"dataset-{i}",
            "category": category,
            "storage_type": "mysql",
            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
        }
        for i, category in enumerate(["BTCUSDT", "BTCUSDT", "ETHUSDT"])
    ]
    with adapter.engine.begin() as conn:
        conn.execute(sa.insert(table), rows)
    return adapter


def _remaining(adapter) -> list[str]:
    table = adapter._get_table("datasets")
    with adapter.engine.connect() as conn:
        return sorted(r.dataset_id for r in conn.execute(sa.select(table)))


class TestMySQLDelete:
    def test_deletes_only_matching_rows(self, sqlite_adapter):
        count = sqlite_adapter.delete("datasets", {"category": "BTCUSDT"})

        assert count == 2
        assert _remaining(sqlite_adapter) == ["d2"]

    def test_all_conditions_must_match(self, sqlite_adapter):
        count = sqlite_adapter.delete(
            "datasets", {"category": "BTCUSDT", "dataset_id": "d1"}
        )

        assert count == 1
        assert _remaining(sqlite_adapter) == ["d0", "d2"]

    def test_unknown_column_matches_nothing_instead_of_widening(self, sqlite_adapter):
        """A typo'd key must not be dropped: that would delete every BTCUSDT row."""
        count = sqlite_adapter.delete(
            "datasets", {"category": "BTCUSDT", "datasetid": "d1"}
        )

        assert count == 0
        assert _remaining(sqlite_adapter) == ["d0", "d1", "d2"]

    @pytest.mark.parametrize(
        ("filter_dict", "match"),
        [
            ({}, "empty filter"),
            ({"$or": "x"}, "flat equality"),
            ({"category": {"$ne": "x"}}, "flat equality"),
        ],
    )
    def test_refuses_unsafe_filters(self, sqlite_adapter, filter_dict, match):
        with pytest.raises(DatabaseError, match=match):
            sqlite_adapter.delete("datasets", filter_dict)
        assert _remaining(sqlite_adapter) == ["d0", "d1", "d2"]

    def test_refuses_when_not_connected(self, sqlite_adapter):
        sqlite_adapter._connected = False

        with pytest.raises(DatabaseError) as exc_info:
            sqlite_adapter.delete("datasets", {"category": "BTCUSDT"})

        assert "Not connected" in str(exc_info.value)

    def test_sql_error_is_wrapped(self, sqlite_adapter):
        sqlite_adapter.engine = Mock()
        sqlite_adapter.engine.begin.side_effect = sa.exc.OperationalError(
            "DELETE", {}, Exception("gone away")
        )

        with pytest.raises(DatabaseError) as exc_info:
            sqlite_adapter.delete("datasets", {"category": "BTCUSDT"})

        assert "Failed to delete from datasets" in str(exc_info.value)
        assert isinstance(exc_info.value.__cause__, sa.exc.OperationalError)
