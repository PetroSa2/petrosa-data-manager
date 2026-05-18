"""
Coverage for MongoDBAdapter methods beyond _prepare_for_bson (which already has
tests). Motor client is mocked at the collection level.
"""

from datetime import UTC, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pymongo.errors import DuplicateKeyError, PyMongoError

from data_manager.db.base_adapter import DatabaseError
from data_manager.db.mongodb_adapter import MongoDBAdapter


@pytest.fixture
def adapter():
    """Build a connected adapter with mocked motor client."""
    a = MongoDBAdapter("mongodb://localhost:27017/test_db")
    a.client = MagicMock()
    a.db = MagicMock()
    a._connected = True
    return a


class TestExtractDbName:
    def test_extracts_db_name_from_uri(self):
        a = MongoDBAdapter("mongodb://localhost:27017/mydb")
        assert a.db_name == "mydb"

    def test_strips_query_params(self):
        a = MongoDBAdapter("mongodb://host:27017/mydb?authSource=admin")
        assert a.db_name == "mydb"

    def test_returns_default_when_no_db(self):
        a = MongoDBAdapter("mongodb://localhost:27017")
        assert a.db_name == "petrosa_data_manager"

    def test_returns_default_when_empty_segment(self):
        a = MongoDBAdapter("mongodb://localhost:27017/")
        assert a.db_name == "petrosa_data_manager"


class TestConnectDisconnect:
    def test_connect_initializes_client(self):
        with patch(
            "data_manager.db.mongodb_adapter.motor_asyncio.AsyncIOMotorClient"
        ) as Cli:
            mock_client = MagicMock()
            Cli.return_value = mock_client
            a = MongoDBAdapter("mongodb://localhost:27017/test_db")
            a.connect()
            assert a._connected is True
            assert a.client is mock_client

    def test_connect_wraps_exception(self):
        with patch(
            "data_manager.db.mongodb_adapter.motor_asyncio.AsyncIOMotorClient",
            side_effect=RuntimeError("can't connect"),
        ):
            a = MongoDBAdapter("mongodb://localhost:27017/test_db")
            with pytest.raises(
                DatabaseError, match="Failed to connect to MongoDB"
            ) as exc_info:
                a.connect()
            assert "Failed to connect" in str(exc_info.value)

    def test_disconnect_clears_state(self):
        a = MongoDBAdapter("mongodb://localhost:27017/test_db")
        a.client = MagicMock()
        a._connected = True
        a.disconnect()
        assert a._connected is False
        a.client.close.assert_called_once()

    def test_disconnect_when_never_connected(self):
        a = MongoDBAdapter("mongodb://localhost:27017/test_db")
        # client is None — disconnect must not raise
        a.disconnect()
        assert a._connected is False


class TestWrite:
    @pytest.mark.asyncio
    async def test_raises_when_not_connected(self, adapter):
        adapter._connected = False
        with pytest.raises(DatabaseError, match="Not connected"):
            await adapter.write([], "any")

    @pytest.mark.asyncio
    async def test_empty_models_returns_zero(self, adapter):
        assert await adapter.write([], "any") == 0

    @pytest.mark.asyncio
    async def test_write_inserts_with_synthetic_id(self, adapter):
        model = MagicMock()
        model.model_dump.return_value = {
            "symbol": "BTCUSDT",
            "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
            "value": Decimal("1.5"),
        }
        coll = MagicMock()
        coll.insert_many = AsyncMock(
            return_value=MagicMock(inserted_ids=["BTCUSDT_1767225600000"])
        )
        adapter.db.__getitem__ = MagicMock(return_value=coll)

        n = await adapter.write([model], "candles_BTCUSDT")
        assert n == 1
        # Verify document had a synthetic _id and Decimal was converted.
        doc = coll.insert_many.call_args[0][0][0]
        assert doc["_id"] == "BTCUSDT_1767225600000"
        assert doc["value"] == 1.5  # Decimal → float

    @pytest.mark.asyncio
    async def test_duplicate_key_error_returns_partial(self, adapter):
        model = MagicMock()
        model.model_dump.return_value = {
            "symbol": "BTCUSDT",
            "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
        }
        # DuplicateKeyError.details is read-only via property; pass via constructor.
        err = DuplicateKeyError("dup", details={"nInserted": 3})
        coll = MagicMock()
        coll.insert_many = AsyncMock(side_effect=err)
        adapter.db.__getitem__ = MagicMock(return_value=coll)
        result = await adapter.write([model, model, model, model, model], "x")
        assert result == 3

    @pytest.mark.asyncio
    async def test_duplicate_key_error_no_details_returns_zero(self, adapter):
        model = MagicMock()
        model.model_dump.return_value = {
            "symbol": "BTCUSDT",
            "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
        }
        err = DuplicateKeyError("dup")
        coll = MagicMock()
        coll.insert_many = AsyncMock(side_effect=err)
        adapter.db.__getitem__ = MagicMock(return_value=coll)
        result = await adapter.write([model], "x")
        assert result == 0

    @pytest.mark.asyncio
    async def test_pymongo_error_with_duplicate_msg_returns_zero(self, adapter):
        model = MagicMock()
        model.model_dump.return_value = {
            "symbol": "BTCUSDT",
            "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
        }
        coll = MagicMock()
        coll.insert_many = AsyncMock(side_effect=PyMongoError("duplicate key error"))
        adapter.db.__getitem__ = MagicMock(return_value=coll)
        assert await adapter.write([model], "x") == 0

    @pytest.mark.asyncio
    async def test_pymongo_error_raises_database_error(self, adapter):
        model = MagicMock()
        model.model_dump.return_value = {
            "symbol": "BTCUSDT",
            "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
        }
        coll = MagicMock()
        coll.insert_many = AsyncMock(side_effect=PyMongoError("connection refused"))
        adapter.db.__getitem__ = MagicMock(return_value=coll)
        with pytest.raises(DatabaseError, match="Failed to write"):
            await adapter.write([model], "x")

    @pytest.mark.asyncio
    async def test_string_timestamp_is_parsed(self, adapter):
        model = MagicMock()
        model.model_dump.return_value = {
            "symbol": "BTCUSDT",
            "timestamp": "2026-01-01T00:00:00+00:00",
        }
        coll = MagicMock()
        coll.insert_many = AsyncMock(return_value=MagicMock(inserted_ids=["x"]))
        adapter.db.__getitem__ = MagicMock(return_value=coll)
        await adapter.write([model], "x")
        doc = coll.insert_many.call_args[0][0][0]
        # Timestamp should have been parsed to datetime
        assert isinstance(doc["timestamp"], datetime)


class TestWriteBatch:
    def test_raises_not_implemented(self):
        a = MongoDBAdapter("mongodb://localhost:27017/test")
        with pytest.raises(NotImplementedError) as exc_info:
            a.write_batch([], "any")
        assert "Use write()" in str(exc_info.value)


class TestQueryRange:
    @pytest.mark.asyncio
    async def test_raises_when_not_connected(self, adapter):
        adapter._connected = False
        with pytest.raises(DatabaseError, match="Not connected"):
            await adapter.query_range(
                "x",
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2026, 1, 2, tzinfo=UTC),
            )

    @pytest.mark.asyncio
    async def test_returns_documents_with_id_stripped(self, adapter):
        cursor = MagicMock()
        cursor.sort.return_value = cursor
        cursor.to_list = AsyncMock(
            return_value=[
                {"_id": "abc", "symbol": "BTCUSDT", "value": 1},
                {"_id": "def", "symbol": "BTCUSDT", "value": 2},
            ]
        )
        coll = MagicMock()
        coll.find.return_value = cursor
        adapter.db.__getitem__ = MagicMock(return_value=coll)

        results = await adapter.query_range(
            "x",
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 2, tzinfo=UTC),
            symbol="BTCUSDT",
        )
        assert len(results) == 2
        assert "_id" not in results[0]
        assert "_id" not in results[1]
        # Query filter contained symbol.
        called_q = coll.find.call_args[0][0]
        assert called_q["symbol"] == "BTCUSDT"

    @pytest.mark.asyncio
    async def test_pymongo_error_raises_database_error(self, adapter):
        coll = MagicMock()
        coll.find.side_effect = PyMongoError("read timeout")
        adapter.db.__getitem__ = MagicMock(return_value=coll)
        with pytest.raises(DatabaseError, match="Failed to query range"):
            await adapter.query_range(
                "x",
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2026, 1, 2, tzinfo=UTC),
            )


class TestQueryLatest:
    @pytest.mark.asyncio
    async def test_strips_id(self, adapter):
        cursor = MagicMock()
        cursor.sort.return_value = cursor
        cursor.limit.return_value = cursor
        cursor.to_list = AsyncMock(return_value=[{"_id": "x", "v": 1}])
        coll = MagicMock()
        coll.find.return_value = cursor
        adapter.db.__getitem__ = MagicMock(return_value=coll)
        result = await adapter.query_latest("x", symbol="BTCUSDT", limit=3)
        assert "_id" not in result[0]

    @pytest.mark.asyncio
    async def test_raises_when_not_connected(self, adapter):
        adapter._connected = False
        with pytest.raises(DatabaseError, match="Not connected"):
            await adapter.query_latest("x")


class TestGetRecordCount:
    @pytest.mark.asyncio
    async def test_returns_count(self, adapter):
        coll = MagicMock()
        coll.count_documents = AsyncMock(return_value=42)
        adapter.db.__getitem__ = MagicMock(return_value=coll)
        assert (
            await adapter.get_record_count(
                "x",
                start=datetime(2026, 1, 1, tzinfo=UTC),
                end=datetime(2026, 1, 2, tzinfo=UTC),
                symbol="BTCUSDT",
            )
            == 42
        )

    @pytest.mark.asyncio
    async def test_raises_when_not_connected(self, adapter):
        adapter._connected = False
        with pytest.raises(DatabaseError, match="Not connected"):
            await adapter.get_record_count("x")

    @pytest.mark.asyncio
    async def test_pymongo_error_raises_database_error(self, adapter):
        coll = MagicMock()
        coll.count_documents = AsyncMock(side_effect=PyMongoError("x"))
        adapter.db.__getitem__ = MagicMock(return_value=coll)
        with pytest.raises(DatabaseError, match="Failed to count"):
            await adapter.get_record_count("x")


class TestEnsureIndexes:
    @pytest.mark.asyncio
    async def test_creates_schema_specific_indexes(self, adapter):
        coll = MagicMock()
        coll.create_indexes = AsyncMock()
        adapter.db.__getitem__ = MagicMock(return_value=coll)
        await adapter.ensure_indexes("schemas")
        coll.create_indexes.assert_called_once()
        # Schemas collection should have 5 indexes.
        indexes = coll.create_indexes.call_args[0][0]
        assert len(indexes) == 5

    @pytest.mark.asyncio
    async def test_creates_default_time_series_indexes(self, adapter):
        coll = MagicMock()
        coll.create_indexes = AsyncMock()
        adapter.db.__getitem__ = MagicMock(return_value=coll)
        await adapter.ensure_indexes("trades_BTCUSDT")
        indexes = coll.create_indexes.call_args[0][0]
        # Default time-series indexes: 2.
        assert len(indexes) == 2

    @pytest.mark.asyncio
    async def test_creates_intents_collection_indexes(self, adapter):
        coll = MagicMock()
        coll.create_indexes = AsyncMock()
        adapter.db.__getitem__ = MagicMock(return_value=coll)
        await adapter.ensure_indexes("intents")
        indexes = coll.create_indexes.call_args[0][0]
        # intent_id (unique), strategy_id, decision_id (sparse), timestamp = 4
        assert len(indexes) == 4

    @pytest.mark.asyncio
    async def test_swallows_pymongo_error(self, adapter):
        coll = MagicMock()
        coll.create_indexes = AsyncMock(side_effect=PyMongoError("x"))
        adapter.db.__getitem__ = MagicMock(return_value=coll)
        # Must not raise.
        await adapter.ensure_indexes("any")

    @pytest.mark.asyncio
    async def test_raises_when_not_connected(self, adapter):
        adapter._connected = False
        with pytest.raises(DatabaseError, match="Not connected"):
            await adapter.ensure_indexes("any")


class TestDeleteRange:
    @pytest.mark.asyncio
    async def test_returns_deleted_count(self, adapter):
        coll = MagicMock()
        coll.delete_many = AsyncMock(return_value=MagicMock(deleted_count=5))
        adapter.db.__getitem__ = MagicMock(return_value=coll)
        result = await adapter.delete_range(
            "x",
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 2, tzinfo=UTC),
            symbol="BTCUSDT",
        )
        assert result == 5

    @pytest.mark.asyncio
    async def test_raises_when_not_connected(self, adapter):
        adapter._connected = False
        with pytest.raises(DatabaseError, match="Not connected"):
            await adapter.delete_range(
                "x",
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2026, 1, 2, tzinfo=UTC),
            )

    @pytest.mark.asyncio
    async def test_pymongo_error_raises_database_error(self, adapter):
        coll = MagicMock()
        coll.delete_many = AsyncMock(side_effect=PyMongoError("x"))
        adapter.db.__getitem__ = MagicMock(return_value=coll)
        with pytest.raises(DatabaseError, match="Failed to delete"):
            await adapter.delete_range(
                "x",
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2026, 1, 2, tzinfo=UTC),
            )


class TestListCollections:
    @pytest.mark.asyncio
    async def test_returns_collection_names(self, adapter):
        adapter.db.list_collection_names = AsyncMock(
            return_value=["candles_BTCUSDT", "trades_BTCUSDT"]
        )
        assert await adapter.list_collections() == [
            "candles_BTCUSDT",
            "trades_BTCUSDT",
        ]

    @pytest.mark.asyncio
    async def test_raises_when_not_connected(self, adapter):
        adapter._connected = False
        with pytest.raises(DatabaseError, match="Not connected"):
            await adapter.list_collections()

    @pytest.mark.asyncio
    async def test_pymongo_error_raises_database_error(self, adapter):
        adapter.db.list_collection_names = AsyncMock(side_effect=PyMongoError("x"))
        with pytest.raises(DatabaseError, match="Failed to list"):
            await adapter.list_collections()


class TestConfigurationMethods:
    @pytest.mark.asyncio
    async def test_get_app_config_returns_none_when_not_connected(self, adapter):
        adapter._connected = False
        assert await adapter.get_app_config() is None

    @pytest.mark.asyncio
    async def test_get_app_config_returns_document(self, adapter):
        adapter.db.app_config.find_one = AsyncMock(return_value={"k": "v"})
        assert await adapter.get_app_config() == {"k": "v"}

    @pytest.mark.asyncio
    async def test_get_app_config_returns_none_on_exception(self, adapter):
        adapter.db.app_config.find_one = AsyncMock(side_effect=RuntimeError("x"))
        assert await adapter.get_app_config() is None

    @pytest.mark.asyncio
    async def test_upsert_app_config_returns_id_for_new(self, adapter):
        adapter.db.app_config.replace_one = AsyncMock(
            return_value=MagicMock(upserted_id="new-id")
        )
        result = await adapter.upsert_app_config({"k": "v"}, {"by": "tester"})
        assert result == "new-id"

    @pytest.mark.asyncio
    async def test_upsert_app_config_returns_updated_for_existing(self, adapter):
        adapter.db.app_config.replace_one = AsyncMock(
            return_value=MagicMock(upserted_id=None)
        )
        assert await adapter.upsert_app_config({"k": "v"}, {}) == "updated"

    @pytest.mark.asyncio
    async def test_upsert_app_config_returns_none_when_disconnected(self, adapter):
        adapter._connected = False
        assert await adapter.upsert_app_config({}, {}) is None

    @pytest.mark.asyncio
    async def test_upsert_app_config_returns_none_on_exception(self, adapter):
        adapter.db.app_config.replace_one = AsyncMock(side_effect=RuntimeError("x"))
        assert await adapter.upsert_app_config({}, {}) is None

    @pytest.mark.asyncio
    async def test_get_global_config(self, adapter):
        adapter.db.strategy_configs_global.find_one = AsyncMock(
            return_value={"strategy_id": "s1"}
        )
        result = await adapter.get_global_config("s1")
        assert result == {"strategy_id": "s1"}

    @pytest.mark.asyncio
    async def test_get_global_config_returns_none_on_exception(self, adapter):
        adapter.db.strategy_configs_global.find_one = AsyncMock(
            side_effect=RuntimeError("x")
        )
        assert await adapter.get_global_config("s1") is None

    @pytest.mark.asyncio
    async def test_upsert_global_config_returns_id(self, adapter):
        adapter.db.strategy_configs_global.replace_one = AsyncMock(
            return_value=MagicMock(upserted_id="new")
        )
        result = await adapter.upsert_global_config("s1", {}, {})
        assert result == "new"

    @pytest.mark.asyncio
    async def test_upsert_global_config_returns_updated(self, adapter):
        adapter.db.strategy_configs_global.replace_one = AsyncMock(
            return_value=MagicMock(upserted_id=None)
        )
        assert await adapter.upsert_global_config("s1", {}, {}) == "updated"

    @pytest.mark.asyncio
    async def test_upsert_global_config_returns_none_on_exception(self, adapter):
        adapter.db.strategy_configs_global.replace_one = AsyncMock(
            side_effect=RuntimeError("x")
        )
        assert await adapter.upsert_global_config("s1", {}, {}) is None

    @pytest.mark.asyncio
    async def test_get_symbol_config(self, adapter):
        adapter.db.strategy_configs_symbol.find_one = AsyncMock(return_value={"k": "v"})
        assert await adapter.get_symbol_config("s1", "BTCUSDT") == {"k": "v"}

    @pytest.mark.asyncio
    async def test_get_symbol_config_returns_none_on_exception(self, adapter):
        adapter.db.strategy_configs_symbol.find_one = AsyncMock(
            side_effect=RuntimeError("x")
        )
        assert await adapter.get_symbol_config("s1", "BTCUSDT") is None

    @pytest.mark.asyncio
    async def test_upsert_symbol_config(self, adapter):
        adapter.db.strategy_configs_symbol.replace_one = AsyncMock(
            return_value=MagicMock(upserted_id=None)
        )
        assert await adapter.upsert_symbol_config("s1", "BTCUSDT", {}, {}) == "updated"

    @pytest.mark.asyncio
    async def test_upsert_symbol_config_returns_none_on_exception(self, adapter):
        adapter.db.strategy_configs_symbol.replace_one = AsyncMock(
            side_effect=RuntimeError("x")
        )
        assert await adapter.upsert_symbol_config("s1", "BTCUSDT", {}, {}) is None

    @pytest.mark.asyncio
    async def test_delete_global_config(self, adapter):
        adapter.db.strategy_configs_global.delete_one = AsyncMock(
            return_value=MagicMock(deleted_count=1)
        )
        assert await adapter.delete_global_config("s1") is True

    @pytest.mark.asyncio
    async def test_delete_global_config_returns_false_on_exception(self, adapter):
        adapter.db.strategy_configs_global.delete_one = AsyncMock(
            side_effect=RuntimeError("x")
        )
        assert await adapter.delete_global_config("s1") is False

    @pytest.mark.asyncio
    async def test_delete_symbol_config(self, adapter):
        adapter.db.strategy_configs_symbol.delete_one = AsyncMock(
            return_value=MagicMock(deleted_count=1)
        )
        assert await adapter.delete_symbol_config("s1", "BTCUSDT") is True

    @pytest.mark.asyncio
    async def test_delete_symbol_config_returns_false_on_exception(self, adapter):
        adapter.db.strategy_configs_symbol.delete_one = AsyncMock(
            side_effect=RuntimeError("x")
        )
        assert await adapter.delete_symbol_config("s1", "BTCUSDT") is False

    @pytest.mark.asyncio
    async def test_list_all_strategy_ids(self, adapter):
        adapter.db.strategy_configs_global.distinct = AsyncMock(
            return_value=["s1", "s2"]
        )
        adapter.db.strategy_configs_symbol.distinct = AsyncMock(
            return_value=["s2", "s3"]
        )
        ids = await adapter.list_all_strategy_ids()
        assert ids == ["s1", "s2", "s3"]

    @pytest.mark.asyncio
    async def test_list_all_strategy_ids_returns_empty_on_exception(self, adapter):
        adapter.db.strategy_configs_global.distinct = AsyncMock(
            side_effect=RuntimeError("x")
        )
        assert await adapter.list_all_strategy_ids() == []
