"""
Unit tests for data_manager.services.schema_service.SchemaService.

Mocks the SchemaRepository; exercises the business logic: cache hits/misses,
JSON schema validation, compatibility analysis (breaking changes / warnings),
version-constraint enforcement.
"""

from datetime import UTC, datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest

import constants
from data_manager.models.schemas import (
    CompatibilityMode,
    SchemaCompatibilityRequest,
    SchemaDefinition,
    SchemaRegistration,
    SchemaStatus,
    SchemaUpdate,
    SchemaValidationRequest,
    SchemaVersion,
)
from data_manager.services.schema_service import SchemaService


def make_def(version: int = 1, schema=None) -> SchemaDefinition:
    return SchemaDefinition(
        name="user",
        version=version,
        schema=schema or {"type": "object", "properties": {"id": {"type": "integer"}}},
        compatibility_mode=CompatibilityMode.BACKWARD,
        status=SchemaStatus.ACTIVE,
        description="user schema",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        created_by="tester",
    )


def make_version(version: int = 1) -> SchemaVersion:
    return SchemaVersion(
        version=version,
        schema={"type": "object"},
        compatibility_mode=CompatibilityMode.BACKWARD,
        status=SchemaStatus.ACTIVE,
        description="v",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        created_by="tester",
    )


@pytest.fixture
def mock_repo():
    repo = Mock()
    repo.get_schema = AsyncMock(return_value=None)
    repo.register_schema = AsyncMock()
    repo.list_schemas = AsyncMock(return_value=([], 0))
    repo.get_schema_versions = AsyncMock(return_value=[])
    repo.update_schema = AsyncMock(return_value=None)
    repo.deprecate_schema = AsyncMock(return_value=False)
    repo.search_schemas = AsyncMock(return_value=[])
    return repo


class TestRegisterSchema:
    @pytest.mark.asyncio
    async def test_registers_when_no_conflict(self, mock_repo):
        mock_repo.get_schema = AsyncMock(return_value=None)
        mock_repo.register_schema = AsyncMock(return_value=make_def())
        service = SchemaService(mock_repo)
        registration = SchemaRegistration(version=1, schema={"type": "object"})
        result = await service.register_schema("mysql", "user", registration)
        assert result.version == 1
        # Cache should now have the entry.
        assert "mysql:user:1" in service._schema_cache

    @pytest.mark.asyncio
    async def test_rejects_duplicate_version(self, mock_repo):
        mock_repo.get_schema = AsyncMock(return_value=make_def(version=1))
        service = SchemaService(mock_repo)
        registration = SchemaRegistration(version=1, schema={"type": "object"})
        with pytest.raises(ValueError, match="already exists") as exc_info:
            await service.register_schema("mysql", "user", registration)
        assert "already exists" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_rejects_invalid_schema(self, mock_repo):
        service = SchemaService(mock_repo)
        registration = SchemaRegistration(
            version=1, schema={"type": "invalid-type-not-real"}
        )
        with pytest.raises(
            ValueError, match="Invalid JSON Schema|Schema validation error"
        ) as exc_info:
            await service.register_schema("mysql", "user", registration)
        assert exc_info.value is not None


class TestGetSchema:
    @pytest.mark.asyncio
    async def test_cache_miss_falls_through_to_repo(self, mock_repo):
        mock_repo.get_schema = AsyncMock(return_value=make_def(version=2))
        service = SchemaService(mock_repo)
        result = await service.get_schema("mysql", "user", version=2)
        assert result is not None
        assert result.version == 2
        # Now cached.
        assert "mysql:user:2" in service._schema_cache

    @pytest.mark.asyncio
    async def test_cache_hit_short_circuits(self, mock_repo, monkeypatch):
        service = SchemaService(mock_repo)
        # Pre-populate cache.
        cached_def = make_def(version=2)
        service._schema_cache["mysql:user:2"] = cached_def
        service._cache_timestamps["mysql:user:2"] = 1.0e12  # very recent
        # Inflate TTL so cache hit succeeds.
        monkeypatch.setattr(constants, "SCHEMA_CACHE_TTL", 999999)
        # Ensure repo would error if called.
        mock_repo.get_schema = AsyncMock(side_effect=AssertionError("should not call"))
        result = await service.get_schema("mysql", "user", version=2)
        assert result is cached_def

    @pytest.mark.asyncio
    async def test_cache_expired_refreshes(self, mock_repo, monkeypatch):
        service = SchemaService(mock_repo)
        service._schema_cache["mysql:user:2"] = make_def(version=2)
        service._cache_timestamps["mysql:user:2"] = 0  # very stale
        monkeypatch.setattr(constants, "SCHEMA_CACHE_TTL", 1)
        # The expired cache forces a fetch from repo.
        new_def = make_def(version=2)
        mock_repo.get_schema = AsyncMock(return_value=new_def)
        result = await service.get_schema("mysql", "user", version=2)
        assert result is new_def

    @pytest.mark.asyncio
    async def test_use_cache_false_bypasses_cache(self, mock_repo):
        service = SchemaService(mock_repo)
        service._schema_cache["mysql:user:1"] = make_def()
        service._cache_timestamps["mysql:user:1"] = 1.0e12
        new_def = make_def()
        mock_repo.get_schema = AsyncMock(return_value=new_def)
        result = await service.get_schema("mysql", "user", version=1, use_cache=False)
        assert result is new_def
        mock_repo.get_schema.assert_called_once()


class TestListSchemas:
    @pytest.mark.asyncio
    async def test_delegates_to_repository(self, mock_repo):
        mock_repo.list_schemas = AsyncMock(return_value=([make_def()], 1))
        service = SchemaService(mock_repo)
        schemas, total = await service.list_schemas(database="mysql", page=1)
        assert total == 1
        mock_repo.list_schemas.assert_called_once()


class TestGetSchemaVersions:
    @pytest.mark.asyncio
    async def test_returns_dicts_from_repo_versions(self, mock_repo):
        mock_repo.get_schema_versions = AsyncMock(
            return_value=[make_version(1), make_version(2)]
        )
        service = SchemaService(mock_repo)
        versions = await service.get_schema_versions("mysql", "user")
        assert len(versions) == 2
        assert versions[0]["version"] == 1
        assert versions[1]["version"] == 2
        # Returned dicts include ISO-formatted timestamps.
        assert isinstance(versions[0]["created_at"], str)


class TestUpdateSchema:
    @pytest.mark.asyncio
    async def test_validates_schema_if_provided(self, mock_repo):
        service = SchemaService(mock_repo)
        update = SchemaUpdate(schema={"type": "invalid-type"})
        with pytest.raises(ValueError) as exc_info:
            await service.update_schema("mysql", "user", 1, update)
        assert exc_info.value is not None

    @pytest.mark.asyncio
    async def test_clears_cache_on_successful_update(self, mock_repo):
        service = SchemaService(mock_repo)
        # Seed cache for the key being updated.
        service._schema_cache["mysql:user:1"] = make_def()
        service._cache_timestamps["mysql:user:1"] = 1.0
        mock_repo.update_schema = AsyncMock(return_value=make_def())
        await service.update_schema(
            "mysql", "user", 1, SchemaUpdate(description="updated")
        )
        assert "mysql:user:1" not in service._schema_cache

    @pytest.mark.asyncio
    async def test_returns_none_when_repository_returns_none(self, mock_repo):
        service = SchemaService(mock_repo)
        mock_repo.update_schema = AsyncMock(return_value=None)
        result = await service.update_schema(
            "mysql", "user", 1, SchemaUpdate(description="x")
        )
        assert result is None


class TestDeprecateSchema:
    @pytest.mark.asyncio
    async def test_clears_cache_on_success(self, mock_repo):
        service = SchemaService(mock_repo)
        service._schema_cache["mysql:user:1"] = make_def()
        service._cache_timestamps["mysql:user:1"] = 1.0
        mock_repo.deprecate_schema = AsyncMock(return_value=True)
        assert await service.deprecate_schema("mysql", "user", 1) is True
        assert "mysql:user:1" not in service._schema_cache

    @pytest.mark.asyncio
    async def test_returns_false_when_repo_returns_false(self, mock_repo):
        service = SchemaService(mock_repo)
        mock_repo.deprecate_schema = AsyncMock(return_value=False)
        assert await service.deprecate_schema("mysql", "user", 1) is False


class TestValidateData:
    @pytest.mark.asyncio
    async def test_validates_passing_data(self, mock_repo):
        mock_repo.get_schema = AsyncMock(
            return_value=make_def(
                schema={
                    "type": "object",
                    "properties": {"id": {"type": "integer"}},
                    "required": ["id"],
                }
            )
        )
        service = SchemaService(mock_repo)
        request = SchemaValidationRequest(
            database="mysql",
            schema_name="user",
            data={"id": 1},
        )
        result = await service.validate_data(request)
        assert result.valid is True
        assert result.validated_count == 1

    @pytest.mark.asyncio
    async def test_invalid_data_returns_errors(self, mock_repo):
        mock_repo.get_schema = AsyncMock(
            return_value=make_def(
                schema={
                    "type": "object",
                    "properties": {"id": {"type": "integer"}},
                    "required": ["id"],
                }
            )
        )
        service = SchemaService(mock_repo)
        request = SchemaValidationRequest(
            database="mysql",
            schema_name="user",
            data={"id": "not-an-int"},
        )
        result = await service.validate_data(request)
        assert result.valid is False
        assert len(result.errors) >= 1

    @pytest.mark.asyncio
    async def test_validates_list_of_items(self, mock_repo):
        mock_repo.get_schema = AsyncMock(
            return_value=make_def(
                schema={"type": "object", "properties": {"id": {"type": "integer"}}}
            )
        )
        service = SchemaService(mock_repo)
        request = SchemaValidationRequest(
            database="mysql",
            schema_name="user",
            data=[{"id": 1}, {"id": 2}, {"id": "bad"}],
        )
        result = await service.validate_data(request)
        # 2 valid, 1 invalid → overall invalid.
        assert result.valid is False
        assert result.validated_count == 2
        assert len(result.errors) == 1

    @pytest.mark.asyncio
    async def test_schema_not_found_returns_invalid(self, mock_repo):
        mock_repo.get_schema = AsyncMock(return_value=None)
        service = SchemaService(mock_repo)
        request = SchemaValidationRequest(
            database="mysql",
            schema_name="missing",
            data={"k": "v"},
        )
        result = await service.validate_data(request)
        assert result.valid is False
        assert any("not found" in e for e in result.errors)

    @pytest.mark.asyncio
    async def test_validation_exception_returns_invalid(self, mock_repo):
        mock_repo.get_schema = AsyncMock(side_effect=RuntimeError("oops"))
        service = SchemaService(mock_repo)
        request = SchemaValidationRequest(
            database="mysql",
            schema_name="user",
            data={"k": "v"},
        )
        result = await service.validate_data(request)
        assert result.valid is False


class TestCheckCompatibility:
    @pytest.mark.asyncio
    async def test_compatible_schemas_no_breaking_changes(self, mock_repo):
        old_schema = make_def(
            version=1,
            schema={
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
            },
        )
        new_schema = make_def(
            version=2,
            schema={
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "name": {"type": "string"},
                },
                "required": ["id"],
            },
        )
        mock_repo.get_schema = AsyncMock(side_effect=[old_schema, new_schema])
        service = SchemaService(mock_repo)
        request = SchemaCompatibilityRequest(
            database="mysql",
            schema_name="user",
            old_version=1,
            new_version=2,
        )
        result = await service.check_compatibility(request)
        assert result.compatible is True
        assert len(result.breaking_changes) == 0

    @pytest.mark.asyncio
    async def test_removed_required_field_is_breaking(self, mock_repo):
        old_schema = make_def(
            version=1,
            schema={
                "type": "object",
                "properties": {"id": {"type": "integer"}, "name": {"type": "string"}},
                "required": ["id", "name"],
            },
        )
        new_schema = make_def(
            version=2,
            schema={
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
            },
        )
        mock_repo.get_schema = AsyncMock(side_effect=[old_schema, new_schema])
        service = SchemaService(mock_repo)
        request = SchemaCompatibilityRequest(
            database="mysql",
            schema_name="user",
            old_version=1,
            new_version=2,
        )
        result = await service.check_compatibility(request)
        assert result.compatible is False
        assert any("name" in bc for bc in result.breaking_changes)

    @pytest.mark.asyncio
    async def test_type_change_is_breaking(self, mock_repo):
        old_schema = make_def(
            version=1,
            schema={"type": "object", "properties": {"id": {"type": "integer"}}},
        )
        new_schema = make_def(
            version=2,
            schema={"type": "object", "properties": {"id": {"type": "string"}}},
        )
        mock_repo.get_schema = AsyncMock(side_effect=[old_schema, new_schema])
        service = SchemaService(mock_repo)
        request = SchemaCompatibilityRequest(
            database="mysql",
            schema_name="user",
            old_version=1,
            new_version=2,
        )
        result = await service.check_compatibility(request)
        assert result.compatible is False
        assert any("type changed" in bc for bc in result.breaking_changes)

    @pytest.mark.asyncio
    async def test_new_required_field_is_warning(self, mock_repo):
        old_schema = make_def(
            version=1,
            schema={
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
            },
        )
        new_schema = make_def(
            version=2,
            schema={
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "email": {"type": "string"},
                },
                "required": ["id", "email"],
            },
        )
        mock_repo.get_schema = AsyncMock(side_effect=[old_schema, new_schema])
        service = SchemaService(mock_repo)
        request = SchemaCompatibilityRequest(
            database="mysql",
            schema_name="user",
            old_version=1,
            new_version=2,
        )
        result = await service.check_compatibility(request)
        # New required field is a warning but not technically breaking per the analyzer.
        assert any("email" in w for w in result.warnings)

    @pytest.mark.asyncio
    async def test_missing_schema_returns_incompatible(self, mock_repo):
        mock_repo.get_schema = AsyncMock(side_effect=[None, None])
        service = SchemaService(mock_repo)
        request = SchemaCompatibilityRequest(
            database="mysql",
            schema_name="missing",
            old_version=1,
            new_version=2,
        )
        result = await service.check_compatibility(request)
        assert result.compatible is False
        assert result.compatibility_mode == "UNKNOWN"

    @pytest.mark.asyncio
    async def test_exception_returns_error_response(self, mock_repo):
        mock_repo.get_schema = AsyncMock(side_effect=RuntimeError("boom"))
        service = SchemaService(mock_repo)
        request = SchemaCompatibilityRequest(
            database="mysql",
            schema_name="user",
            old_version=1,
            new_version=2,
        )
        result = await service.check_compatibility(request)
        assert result.compatible is False
        assert result.compatibility_mode == "ERROR"


class TestSearchSchemas:
    @pytest.mark.asyncio
    async def test_returns_dicts_from_repo_results(self, mock_repo):
        mock_repo.search_schemas = AsyncMock(return_value=[make_def()])
        service = SchemaService(mock_repo)
        result = await service.search_schemas("user", database="mysql")
        assert len(result) == 1
        assert result[0]["name"] == "user"
        assert result[0]["database"] == "mysql"

    @pytest.mark.asyncio
    async def test_falls_back_to_unknown_db_label(self, mock_repo):
        mock_repo.search_schemas = AsyncMock(return_value=[make_def()])
        service = SchemaService(mock_repo)
        result = await service.search_schemas("user", database=None)
        assert result[0]["database"] == "unknown"


class TestValidateVersionConstraints:
    @pytest.mark.asyncio
    async def test_raises_when_max_versions_reached(self, mock_repo, monkeypatch):
        # Force a low max so any existing versions trip the guard.
        monkeypatch.setattr(constants, "SCHEMA_MAX_VERSIONS", 1)
        mock_repo.get_schema = AsyncMock(return_value=None)
        mock_repo.get_schema_versions = AsyncMock(return_value=[make_version(1)])
        service = SchemaService(mock_repo)
        registration = SchemaRegistration(version=2, schema={"type": "object"})
        with pytest.raises(ValueError, match="Maximum schema versions") as exc_info:
            await service.register_schema("mysql", "user", registration)
        assert "Maximum" in str(exc_info.value)


class TestCacheManagement:
    def test_clear_cache_empties_both_dicts(self):
        service = SchemaService(Mock())
        service._schema_cache["k"] = make_def()
        service._cache_timestamps["k"] = 1.0
        service.clear_cache()
        assert len(service._schema_cache) == 0
        assert len(service._cache_timestamps) == 0

    def test_get_cache_stats_returns_size_and_keys(self):
        service = SchemaService(Mock())
        service._schema_cache["mysql:user:1"] = make_def()
        stats = service.get_cache_stats()
        assert stats["cache_size"] == 1
        assert "mysql:user:1" in stats["cached_schemas"]
        assert "cache_ttl_seconds" in stats
