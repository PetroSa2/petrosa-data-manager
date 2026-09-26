"""PUT /api/v1/{database}/{collection} upserts are atomic (data-manager#378, G2/G5).

MongoDB: one ``update_one(..., upsert=True)``, no ``query_range`` scan.
MySQL: the blocking ``update``/``write`` calls run off the event loop.
"""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import DuplicateKeyError, PyMongoError

import data_manager.api.app as api_module
from data_manager.db.base_adapter import DatabaseError
from data_manager.db.mongodb_adapter import MongoDBAdapter
from data_manager.db.mysql_adapter import WriteResult

POSITION_COLUMNS = {
    "position_id",
    "symbol",
    "quantity",
    "status",
    "created_at",
    "updated_at",
}


def _on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@pytest.fixture
def mongo_adapter():
    """A real MongoDBAdapter whose Motor collection is a mock."""
    adapter = MongoDBAdapter("mongodb://localhost:27017/test_db")
    adapter.client = MagicMock()
    adapter.db = MagicMock()
    adapter._connected = True
    coll = MagicMock()
    coll.update_one = AsyncMock(
        return_value=Mock(matched_count=0, modified_count=0, upserted_id="new-id")
    )
    adapter.db.__getitem__ = MagicMock(return_value=coll)
    adapter.query_range = AsyncMock(return_value=[])
    return adapter


@pytest.fixture
def client(mock_db_manager, mongo_adapter):
    mock_db_manager.mongodb_adapter = mongo_adapter
    mysql = Mock()
    mysql.query_range = Mock(return_value=[])
    mysql.get_column_names = Mock(return_value=POSITION_COLUMNS)
    mysql.update = Mock(return_value=0)
    mysql.write = Mock(return_value=WriteResult(inserted=1))
    mock_db_manager.mysql_adapter = mysql
    api_module.db_manager = mock_db_manager
    yield TestClient(api_module.create_app())
    api_module.db_manager = None


def _coll(adapter):
    return adapter.db.__getitem__.return_value


class TestMongoUpsertRoute:
    def test_upsert_is_one_update_one_with_upsert_true_and_no_scan(
        self, client, mongo_adapter
    ):
        response = client.put(
            "/api/v1/mongodb/heartbeat_state",
            json={"filter": {"service": "te"}, "data": {"beat": 7}, "upsert": True},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["updated_count"] == 1
        assert body["upserted"] is True
        coll = _coll(mongo_adapter)
        coll.update_one.assert_awaited_once()
        query, update = coll.update_one.await_args.args
        assert query == {"service": "te"}
        assert update["$set"]["beat"] == 7
        assert isinstance(update["$set"]["updated_at"], datetime)
        assert isinstance(update["$setOnInsert"]["created_at"], datetime)
        assert coll.update_one.await_args.kwargs == {"upsert": True}
        mongo_adapter.query_range.assert_not_called()
        coll.find.assert_not_called()

    def test_upsert_matching_existing_document_is_an_update(
        self, client, mongo_adapter
    ):
        _coll(mongo_adapter).update_one.return_value = Mock(
            matched_count=1, modified_count=1, upserted_id=None
        )

        response = client.put(
            "/api/v1/mongodb/heartbeat_state",
            json={"filter": {"service": "te"}, "data": {"beat": 8}, "upsert": True},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["updated_count"] == 1
        assert body["upserted"] is False
        # Response keys stay backward compatible for tradeengine.
        assert {"message", "updated_count", "upserted", "metadata"} <= body.keys()

    def test_caller_created_at_is_set_without_conflicting_set_on_insert(
        self, client, mongo_adapter
    ):
        response = client.put(
            "/api/v1/mongodb/heartbeat_state",
            json={
                "filter": {"service": "te"},
                "data": {"beat": 1, "created_at": "2026-09-01T00:00:00+00:00"},
                "upsert": True,
            },
        )

        assert response.status_code == 200
        _query, update = _coll(mongo_adapter).update_one.await_args.args
        assert update["$set"]["created_at"] == "2026-09-01T00:00:00+00:00"
        assert "$setOnInsert" not in update

    def test_operator_filter_is_rejected_before_touching_mongo(
        self, client, mongo_adapter
    ):
        response = client.put(
            "/api/v1/mongodb/heartbeat_state",
            json={
                "filter": {"service": {"$ne": "x"}},
                "data": {"beat": 1},
                "upsert": True,
            },
        )

        assert response.status_code == 400
        assert "flat equality" in response.json()["detail"]
        _coll(mongo_adapter).update_one.assert_not_called()

    @pytest.mark.parametrize("database", ["mongodb", "mysql"])
    def test_empty_filter_is_rejected_with_400(self, client, database):
        response = client.put(
            f"/api/v1/{database}/positions",
            json={"filter": {}, "data": {"status": "open"}, "upsert": True},
        )

        assert response.status_code == 400
        assert "cannot be empty" in response.json()["detail"]

    def test_mongo_failure_is_a_500(self, client, mongo_adapter):
        _coll(mongo_adapter).update_one.side_effect = PyMongoError("down")

        response = client.put(
            "/api/v1/mongodb/heartbeat_state",
            json={"filter": {"service": "te"}, "data": {"beat": 1}, "upsert": True},
        )

        assert response.status_code == 500
        api_module.db_manager.increment_error_count.assert_called_with("mongodb")


class TestMySQLUpsertRoute:
    def test_update_and_write_only_run_through_to_thread(self, client):
        mysql = api_module.db_manager.mysql_adapter
        offloaded = []
        real_to_thread = asyncio.to_thread

        async def recording_to_thread(func, /, *args, **kwargs):
            offloaded.append(func)
            return await real_to_thread(func, *args, **kwargs)

        with patch(
            "data_manager.api.routes.generic.asyncio.to_thread",
            side_effect=recording_to_thread,
        ) as to_thread:
            response = client.put(
                "/api/v1/mysql/positions",
                json={
                    "filter": {"position_id": "p1"},
                    "data": {"symbol": "BTCUSDT", "quantity": 2.0},
                    "upsert": True,
                },
            )

        assert response.status_code == 200
        assert to_thread.called
        assert mysql.update in offloaded
        assert mysql.write in offloaded
        assert offloaded.count(mysql.update) == mysql.update.call_count == 1
        assert offloaded.count(mysql.write) == mysql.write.call_count == 1

    def test_blocking_calls_never_run_on_the_event_loop_thread(self, client):
        mysql = api_module.db_manager.mysql_adapter
        seen_on_loop = []

        def record(result):
            def side_effect(*_args, **_kwargs):
                seen_on_loop.append(_on_event_loop())
                return result

            return side_effect

        mysql.get_column_names.side_effect = record(POSITION_COLUMNS)
        mysql.update.side_effect = record(0)
        mysql.write.side_effect = record(WriteResult(inserted=1))

        response = client.put(
            "/api/v1/mysql/positions",
            json={
                "filter": {"position_id": "p1"},
                "data": {"quantity": 2.0},
                "upsert": True,
            },
        )

        assert response.status_code == 200
        assert seen_on_loop == [False, False, False]

    def test_insert_carries_filter_fields_and_timestamps(self, client):
        mysql = api_module.db_manager.mysql_adapter

        response = client.put(
            "/api/v1/mysql/positions",
            json={
                "filter": {"position_id": "p1"},
                "data": {"symbol": "BTCUSDT", "quantity": 2.0},
                "upsert": True,
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["updated_count"] == 1
        assert body["upserted"] is True
        (models, collection), _ = mysql.write.call_args
        assert collection == "positions"
        record = models[0].model_dump()
        assert record["position_id"] == "p1"
        assert record["symbol"] == "BTCUSDT"
        assert isinstance(record["created_at"], datetime)
        assert isinstance(record["updated_at"], datetime)

    def test_existing_row_is_updated_without_insert(self, client):
        mysql = api_module.db_manager.mysql_adapter
        mysql.update.return_value = 1

        response = client.put(
            "/api/v1/mysql/positions",
            json={
                "filter": {"position_id": "p1"},
                "data": {"quantity": 3.0},
                "upsert": True,
            },
        )

        assert response.status_code == 200
        assert response.json()["upserted"] is False
        mysql.write.assert_not_called()

    def test_concurrent_insert_duplicate_falls_back_to_update(self, client):
        """Lost the INSERT race: the row now exists, so the write lands as an UPDATE."""
        mysql = api_module.db_manager.mysql_adapter
        mysql.update.side_effect = [0, 1]
        mysql.write.return_value = WriteResult(inserted=0, duplicates=1)

        response = client.put(
            "/api/v1/mysql/positions",
            json={
                "filter": {"position_id": "p1"},
                "data": {"quantity": 3.0},
                "upsert": True,
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["updated_count"] == 1
        assert body["upserted"] is False
        assert mysql.update.call_count == 2

    def test_failed_insert_is_a_500_not_a_silent_zero(self, client):
        mysql = api_module.db_manager.mysql_adapter
        mysql.write.return_value = WriteResult(inserted=0, duplicates=0, failed=1)

        response = client.put(
            "/api/v1/mysql/positions",
            json={
                "filter": {"position_id": "p1"},
                "data": {"quantity": 3.0},
                "upsert": True,
            },
        )

        assert response.status_code == 500
        assert "1 failed" in response.json()["detail"]

    def test_unknown_fields_are_reported_on_the_insert_path(self, client):
        response = client.put(
            "/api/v1/mysql/positions",
            json={
                "filter": {"position_id": "p1"},
                "data": {"quantity": 3.0, "avg_price": 10.0},
                "upsert": True,
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["upserted"] is True
        assert body["ignored_fields"] == ["avg_price"]


class TestMongoUpsertOneAdapter:
    @pytest.mark.asyncio
    async def test_duplicate_key_race_is_retried_once(self, mongo_adapter):
        coll = _coll(mongo_adapter)
        coll.update_one.side_effect = [
            DuplicateKeyError("E11000"),
            Mock(matched_count=1, modified_count=1, upserted_id=None),
        ]

        result = await mongo_adapter.upsert_one(
            "heartbeat_state", {"service": "te"}, {"beat": 1}
        )

        assert result == {"matched": 1, "modified": 1, "upserted_id": None}
        assert coll.update_one.await_count == 2

    @pytest.mark.asyncio
    async def test_persistent_duplicate_key_raises(self, mongo_adapter):
        _coll(mongo_adapter).update_one.side_effect = DuplicateKeyError("E11000")

        with pytest.raises(DatabaseError, match="Failed to upsert"):
            await mongo_adapter.upsert_one("positions", {"position_id": "p"}, {"a": 1})

    @pytest.mark.asyncio
    async def test_upserted_id_is_stringified(self, mongo_adapter):
        from bson import ObjectId

        oid = ObjectId()
        _coll(mongo_adapter).update_one.return_value = Mock(
            matched_count=0, modified_count=0, upserted_id=oid
        )

        result = await mongo_adapter.upsert_one("c", {"k": 1}, {"v": 2})

        assert result["upserted_id"] == str(oid)

    @pytest.mark.asyncio
    async def test_decimal_values_are_bson_safe(self, mongo_adapter):
        from decimal import Decimal

        await mongo_adapter.upsert_one("c", {"k": 1}, {"v": Decimal("1.5")})

        _query, update = _coll(mongo_adapter).update_one.await_args.args
        assert update["$set"]["v"] == 1.5

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("filter_dict", "match"),
        [({}, "empty filter"), ({"$where": "1"}, "flat equality")],
    )
    async def test_refuses_unsafe_filters(self, mongo_adapter, filter_dict, match):
        with pytest.raises(DatabaseError, match=match):
            await mongo_adapter.upsert_one("c", filter_dict, {"v": 1})
        _coll(mongo_adapter).update_one.assert_not_called()

    @pytest.mark.asyncio
    async def test_refuses_when_not_connected(self, mongo_adapter):
        mongo_adapter._connected = False

        with pytest.raises(DatabaseError, match="Not connected"):
            await mongo_adapter.upsert_one("c", {"k": 1}, {"v": 1})

    @pytest.mark.asyncio
    async def test_created_at_is_only_stamped_on_insert(self, mongo_adapter):
        before = datetime.now(UTC)

        await mongo_adapter.upsert_one("c", {"k": 1}, {"v": 1})

        _query, update = _coll(mongo_adapter).update_one.await_args.args
        assert "created_at" not in update["$set"]
        assert update["$setOnInsert"]["created_at"] >= before
