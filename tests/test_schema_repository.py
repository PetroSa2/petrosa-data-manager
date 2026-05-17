"""
Coverage for data_manager.db.repositories.schema_repository.SchemaRepository.

Tests both the public dispatchers (mysql/mongodb/raises-for-unknown) and the
private MySQL/MongoDB-specific implementations. Adapters are mocked.
"""

import json
from datetime import UTC, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from data_manager.db.repositories.schema_repository import SchemaRepository
from data_manager.models.schemas import (
    CompatibilityMode,
    SchemaRegistration,
    SchemaStatus,
    SchemaUpdate,
)


def sample_schema_row(name: str = "user", version: int = 1) -> dict:
    return {
        "name": name,
        "version": version,
        "schema_json": json.dumps({"type": "object"}),
        "compatibility_mode": "BACKWARD",
        "status": "ACTIVE",
        "description": "user schema",
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
        "created_by": "tester",
    }


def sample_mongo_doc(name: str = "user", version: int = 1) -> dict:
    return {
        "_id": f"{name}_{version}_abc",
        "name": name,
        "version": version,
        "schema": {"type": "object"},
        "compatibility_mode": "BACKWARD",
        "status": "ACTIVE",
        "description": "user schema",
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
        "created_by": "tester",
    }


def make_registration(version: int = 1) -> SchemaRegistration:
    return SchemaRegistration(
        version=version,
        schema={"type": "object"},
        description="user",
        created_by="tester",
        compatibility_mode=CompatibilityMode.BACKWARD,
    )


class TestRegisterSchemaDispatch:
    @pytest.mark.asyncio
    async def test_mysql_routes_to_register_mysql(self):
        mysql = Mock()
        mysql.write = Mock()
        repo = SchemaRepository(mysql_adapter=mysql, mongodb_adapter=Mock())
        result = await repo.register_schema("mysql", "user", make_registration())
        assert result.name == "user"
        assert mysql.write.called

    @pytest.mark.asyncio
    async def test_mongodb_routes_to_register_mongodb(self):
        mongodb = Mock()
        mongodb.write = AsyncMock(return_value=1)
        repo = SchemaRepository(mysql_adapter=Mock(), mongodb_adapter=mongodb)
        result = await repo.register_schema("mongodb", "user", make_registration())
        assert result.name == "user"
        mongodb.write.assert_called_once()

    @pytest.mark.asyncio
    async def test_unsupported_database_raises(self):
        repo = SchemaRepository(mysql_adapter=Mock(), mongodb_adapter=Mock())
        with pytest.raises(ValueError, match="Unsupported database") as exc_info:
            await repo.register_schema("oracle", "user", make_registration())
        assert "oracle" in str(exc_info.value)


class TestGetSchemaDispatch:
    @pytest.mark.asyncio
    async def test_mysql_returns_definition(self):
        mysql = Mock()
        mysql.query_range = Mock(return_value=[sample_schema_row()])
        repo = SchemaRepository(mysql_adapter=mysql, mongodb_adapter=Mock())
        result = await repo.get_schema("mysql", "user")
        assert result is not None
        assert result.name == "user"
        assert result.version == 1

    @pytest.mark.asyncio
    async def test_mysql_specific_version(self):
        mysql = Mock()
        mysql.query_range = Mock(
            return_value=[sample_schema_row("user", 1), sample_schema_row("user", 2)]
        )
        repo = SchemaRepository(mysql_adapter=mysql, mongodb_adapter=Mock())
        result = await repo.get_schema("mysql", "user", version=2)
        assert result is not None
        assert result.version == 2

    @pytest.mark.asyncio
    async def test_mysql_returns_none_when_not_found(self):
        mysql = Mock()
        mysql.query_range = Mock(return_value=[])
        repo = SchemaRepository(mysql_adapter=mysql, mongodb_adapter=Mock())
        assert await repo.get_schema("mysql", "missing") is None

    @pytest.mark.asyncio
    async def test_mysql_returns_none_on_exception(self):
        mysql = Mock()
        mysql.query_range = Mock(side_effect=RuntimeError("x"))
        repo = SchemaRepository(mysql_adapter=mysql, mongodb_adapter=Mock())
        assert await repo.get_schema("mysql", "user") is None

    @pytest.mark.asyncio
    async def test_unsupported_database_raises(self):
        repo = SchemaRepository(mysql_adapter=Mock(), mongodb_adapter=Mock())
        with pytest.raises(ValueError, match="Unsupported database"):
            await repo.get_schema("unknown", "user")


class TestListSchemas:
    @pytest.mark.asyncio
    async def test_lists_mysql_and_mongodb_when_no_filter(self):
        mysql = Mock()
        mysql.query_range = Mock(return_value=[sample_schema_row()])
        mongodb = Mock()
        mongodb.query_range = AsyncMock(return_value=[sample_mongo_doc()])

        repo = SchemaRepository(mysql_adapter=mysql, mongodb_adapter=mongodb)
        schemas, total = await repo.list_schemas()
        # Both DBs queried; each returned 1 schema.
        assert total >= 1
        assert isinstance(schemas, list)
        # mysql and mongodb adapters were both consulted.
        mysql.query_range.assert_called()
        mongodb.query_range.assert_called()

    @pytest.mark.asyncio
    async def test_lists_mysql_only_with_filter(self):
        mysql = Mock()
        mysql.query_range = Mock(return_value=[sample_schema_row()])
        repo = SchemaRepository(mysql_adapter=mysql, mongodb_adapter=Mock())
        schemas, total = await repo.list_schemas(database="mysql")
        assert total == 1

    @pytest.mark.asyncio
    async def test_list_filters_by_name_pattern(self):
        mysql = Mock()
        mysql.query_range = Mock(
            return_value=[
                sample_schema_row("user"),
                sample_schema_row("order"),
                sample_schema_row("user_profile"),
            ]
        )
        repo = SchemaRepository(mysql_adapter=mysql, mongodb_adapter=Mock())
        schemas, total = await repo.list_schemas(database="mysql", name_pattern="user")
        # 2 schemas match "user" pattern
        assert total == 2

    @pytest.mark.asyncio
    async def test_list_returns_empty_on_mysql_exception(self):
        mysql = Mock()
        mysql.query_range = Mock(side_effect=RuntimeError("x"))
        repo = SchemaRepository(mysql_adapter=mysql, mongodb_adapter=Mock())
        schemas, total = await repo.list_schemas(database="mysql")
        assert schemas == []
        assert total == 0


class TestGetSchemaVersionsDispatch:
    @pytest.mark.asyncio
    async def test_mysql_returns_sorted_versions(self):
        mysql = Mock()
        mysql.query_range = Mock(
            return_value=[
                sample_schema_row("user", 3),
                sample_schema_row("user", 1),
                sample_schema_row("user", 2),
            ]
        )
        repo = SchemaRepository(mysql_adapter=mysql, mongodb_adapter=Mock())
        versions = await repo.get_schema_versions("mysql", "user")
        assert [v.version for v in versions] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_mysql_returns_empty_on_exception(self):
        mysql = Mock()
        mysql.query_range = Mock(side_effect=RuntimeError("x"))
        repo = SchemaRepository(mysql_adapter=mysql, mongodb_adapter=Mock())
        assert await repo.get_schema_versions("mysql", "user") == []

    @pytest.mark.asyncio
    async def test_unsupported_database_raises(self):
        repo = SchemaRepository(mysql_adapter=Mock(), mongodb_adapter=Mock())
        with pytest.raises(ValueError, match="Unsupported database"):
            await repo.get_schema_versions("unknown", "user")


class TestUpdateSchemaDispatch:
    @pytest.mark.asyncio
    async def test_mysql_returns_none_unimplemented(self):
        # Per comments, MySQL update returns None as not implemented.
        repo = SchemaRepository(mysql_adapter=Mock(), mongodb_adapter=Mock())
        result = await repo.update_schema(
            "mysql", "user", 1, SchemaUpdate(description="updated")
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_unsupported_database_raises(self):
        repo = SchemaRepository(mysql_adapter=Mock(), mongodb_adapter=Mock())
        with pytest.raises(ValueError, match="Unsupported database"):
            await repo.update_schema("oracle", "user", 1, SchemaUpdate())


class TestDeprecateSchemaDispatch:
    @pytest.mark.asyncio
    async def test_mysql_returns_false_unimplemented(self):
        repo = SchemaRepository(mysql_adapter=Mock(), mongodb_adapter=Mock())
        assert await repo.deprecate_schema("mysql", "user", 1) is False

    @pytest.mark.asyncio
    async def test_unsupported_database_raises(self):
        repo = SchemaRepository(mysql_adapter=Mock(), mongodb_adapter=Mock())
        with pytest.raises(ValueError, match="Unsupported database"):
            await repo.deprecate_schema("redis", "user", 1)


class TestSearchSchemas:
    @pytest.mark.asyncio
    async def test_mysql_search_matches_name(self):
        # Both rows share "user schema" description, so a "user" query matches both
        # via the description-substring path. To isolate name-match only, use distinct
        # descriptions.
        row_user = {**sample_schema_row("user"), "description": "primary user data"}
        row_order = {**sample_schema_row("order"), "description": "order data"}
        mysql = Mock()
        mysql.query_range = Mock(return_value=[row_user, row_order])
        repo = SchemaRepository(mysql_adapter=mysql, mongodb_adapter=Mock())
        result = await repo.search_schemas("user", database="mysql")
        assert len(result) == 1
        assert result[0].name == "user"

    @pytest.mark.asyncio
    async def test_search_returns_empty_on_mysql_exception(self):
        mysql = Mock()
        mysql.query_range = Mock(side_effect=RuntimeError("x"))
        repo = SchemaRepository(mysql_adapter=mysql, mongodb_adapter=Mock())
        assert await repo.search_schemas("user", database="mysql") == []


class TestRegisterMysqlSchemaErrorPropagation:
    @pytest.mark.asyncio
    async def test_propagates_write_exception(self):
        mysql = Mock()
        mysql.write = Mock(side_effect=RuntimeError("write failed"))
        repo = SchemaRepository(mysql_adapter=mysql, mongodb_adapter=Mock())
        with pytest.raises(RuntimeError, match="write failed"):
            await repo.register_schema("mysql", "user", make_registration())


class TestMongoDBSchemaPath:
    """The MongoDB private methods use `mongodb_adapter.query_range`, NOT a raw
    motor collection. Same adapter shape as MySQL, just async."""

    @pytest.mark.asyncio
    async def test_get_mongodb_schema_specific_version(self):
        mongodb = Mock()
        mongodb.query_range = AsyncMock(
            return_value=[sample_mongo_doc("user", 1), sample_mongo_doc("user", 2)]
        )
        repo = SchemaRepository(mysql_adapter=Mock(), mongodb_adapter=mongodb)
        result = await repo.get_schema("mongodb", "user", version=2)
        assert result is not None
        assert result.version == 2

    @pytest.mark.asyncio
    async def test_get_mongodb_schema_latest(self):
        mongodb = Mock()
        mongodb.query_range = AsyncMock(
            return_value=[
                sample_mongo_doc("user", 1),
                sample_mongo_doc("user", 3),
                sample_mongo_doc("user", 2),
            ]
        )
        repo = SchemaRepository(mysql_adapter=Mock(), mongodb_adapter=mongodb)
        result = await repo.get_schema("mongodb", "user")
        # Latest = max version = 3
        assert result is not None
        assert result.version == 3

    @pytest.mark.asyncio
    async def test_get_mongodb_schema_returns_none_when_missing(self):
        mongodb = Mock()
        mongodb.query_range = AsyncMock(return_value=[])
        repo = SchemaRepository(mysql_adapter=Mock(), mongodb_adapter=mongodb)
        assert await repo.get_schema("mongodb", "missing", version=1) is None

    @pytest.mark.asyncio
    async def test_get_mongodb_schema_returns_none_on_exception(self):
        mongodb = Mock()
        mongodb.query_range = AsyncMock(side_effect=RuntimeError("x"))
        repo = SchemaRepository(mysql_adapter=Mock(), mongodb_adapter=mongodb)
        assert await repo.get_schema("mongodb", "user", version=1) is None

    @pytest.mark.asyncio
    async def test_list_mongodb_schemas_paginates(self):
        mongodb = Mock()
        mongodb.query_range = AsyncMock(
            return_value=[sample_mongo_doc("user", v) for v in range(1, 6)]
        )
        repo = SchemaRepository(mysql_adapter=Mock(), mongodb_adapter=mongodb)
        schemas, total = await repo.list_schemas(
            database="mongodb", page=1, page_size=2
        )
        # 5 schemas total, page 1 of size 2 returns 2.
        assert total == 5
        assert len(schemas) == 2

    @pytest.mark.asyncio
    async def test_list_mongodb_schemas_returns_empty_on_exception(self):
        mongodb = Mock()
        mongodb.query_range = AsyncMock(side_effect=RuntimeError("x"))
        repo = SchemaRepository(mysql_adapter=Mock(), mongodb_adapter=mongodb)
        schemas, total = await repo.list_schemas(database="mongodb")
        assert schemas == []
        assert total == 0

    @pytest.mark.asyncio
    async def test_get_mongodb_schema_versions(self):
        mongodb = Mock()
        mongodb.query_range = AsyncMock(
            return_value=[
                sample_mongo_doc("user", 2),
                sample_mongo_doc("user", 1),
            ]
        )
        repo = SchemaRepository(mysql_adapter=Mock(), mongodb_adapter=mongodb)
        versions = await repo.get_schema_versions("mongodb", "user")
        assert len(versions) == 2

    @pytest.mark.asyncio
    async def test_register_mongodb_schema_propagates_exception(self):
        mongodb = Mock()
        mongodb.write = AsyncMock(side_effect=RuntimeError("write failed"))
        repo = SchemaRepository(mysql_adapter=Mock(), mongodb_adapter=mongodb)
        with pytest.raises(RuntimeError, match="write failed"):
            await repo.register_schema("mongodb", "user", make_registration())

    @pytest.mark.asyncio
    async def test_search_mongodb_schemas(self):
        mongodb = Mock()
        mongodb.query_range = AsyncMock(
            return_value=[
                {**sample_mongo_doc("user"), "description": "primary user data"},
                {**sample_mongo_doc("order"), "description": "order data"},
            ]
        )
        repo = SchemaRepository(mysql_adapter=Mock(), mongodb_adapter=mongodb)
        result = await repo.search_schemas("user", database="mongodb")
        assert len(result) == 1
