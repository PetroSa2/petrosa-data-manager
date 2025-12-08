"""
Tests for schema registry API endpoints.
"""

from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi.testclient import TestClient

import data_manager.api.app as api_module
from data_manager.models.schemas import (
    SchemaBootstrapRequest,
    SchemaStatus,
)


@pytest.fixture
def client(mock_db_manager):
    """Create test client with mocked database."""
    app = api_module.create_app()
    api_module.db_manager = mock_db_manager
    yield TestClient(app)
    api_module.db_manager = None


@pytest.fixture
def mock_schema_service():
    """Mock schema service for testing."""
    from data_manager.models.schemas import SchemaDefinition
    from data_manager.services.schema_service import SchemaService

    mock_service = Mock(spec=SchemaService)

    # Mock schema definition
    mock_schema_def = Mock(spec=SchemaDefinition)
    mock_schema_def.name = "test_schema"
    mock_schema_def.version = 1
    mock_schema_def.status = SchemaStatus.ACTIVE
    mock_schema_def.compatibility_mode = "BACKWARD"
    mock_schema_def.description = "Test schema"
    mock_schema_def.created_at = datetime.utcnow()
    mock_schema_def.updated_at = datetime.utcnow()
    mock_schema_def.created_by = "test_user"
    mock_schema_def.schema = {"type": "object", "properties": {"id": {"type": "integer"}}}

    mock_service.register_schema = AsyncMock(return_value=mock_schema_def)
    mock_service.get_schema = AsyncMock(return_value=mock_schema_def)
    mock_service.get_schema_versions = AsyncMock(return_value=[{"version": 1, "status": "active"}])
    mock_service.update_schema = AsyncMock(return_value=mock_schema_def)
    mock_service.deprecate_schema = AsyncMock(return_value=True)
    mock_service.list_schemas = AsyncMock(return_value=([mock_schema_def], 1))
    mock_service.validate_data = AsyncMock(return_value={"valid": True, "errors": []})
    mock_service.check_compatibility = AsyncMock(return_value={"compatible": True})
    mock_service.search_schemas = AsyncMock(return_value=[])
    mock_service.get_cache_stats = Mock(return_value={"hits": 10, "misses": 5})
    mock_service.clear_cache = Mock()

    return mock_service


@pytest.mark.unit
def test_register_schema_success(client, mock_schema_service):
    """Test successful schema registration."""
    with patch("data_manager.api.routes.schemas.get_schema_service", return_value=mock_schema_service):
        registration = {
            "version": 1,
            "schema": {"type": "object", "properties": {"id": {"type": "integer"}}},
            "compatibility_mode": "BACKWARD",
            "description": "Test schema",
            "created_by": "test_user"
        }
        response = client.post("/schemas/mysql/test_schema", json=registration)
        assert response.status_code == 200
        data = response.json()
        assert "message" in data
        assert "schema" in data
        assert data["schema"]["name"] == "test_schema"


@pytest.mark.unit
def test_register_schema_invalid_database(client):
    """Test schema registration with invalid database."""
    registration = {"version": 1, "schema": {}}
    response = client.post("/schemas/invalid/test_schema", json=registration)
    assert response.status_code == 400
    assert "mysql or mongodb" in response.json()["detail"].lower()


@pytest.mark.unit
def test_get_schema_success(client, mock_schema_service):
    """Test getting schema successfully."""
    with patch("data_manager.api.routes.schemas.get_schema_service", return_value=mock_schema_service):
        response = client.get("/schemas/mysql/test_schema")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "test_schema"
        assert data["version"] == 1


@pytest.mark.unit
def test_get_schema_not_found(client, mock_schema_service):
    """Test getting non-existent schema."""
    mock_schema_service.get_schema = AsyncMock(return_value=None)
    with patch("data_manager.api.routes.schemas.get_schema_service", return_value=mock_schema_service):
        response = client.get("/schemas/mysql/nonexistent")
        assert response.status_code == 404


@pytest.mark.unit
def test_get_schema_versions(client, mock_schema_service):
    """Test getting schema versions."""
    with patch("data_manager.api.routes.schemas.get_schema_service", return_value=mock_schema_service):
        response = client.get("/schemas/mysql/test_schema/versions")
        assert response.status_code == 200
        data = response.json()
        assert "versions" in data
        assert "total_versions" in data


@pytest.mark.unit
def test_update_schema(client, mock_schema_service):
    """Test updating schema."""
    with patch("data_manager.api.routes.schemas.get_schema_service", return_value=mock_schema_service):
        update = {"description": "Updated description", "status": "ACTIVE"}
        response = client.put("/schemas/mysql/test_schema/versions/1", json=update)
        assert response.status_code == 200
        data = response.json()
        assert "message" in data


@pytest.mark.unit
def test_deprecate_schema(client, mock_schema_service):
    """Test deprecating schema."""
    with patch("data_manager.api.routes.schemas.get_schema_service", return_value=mock_schema_service):
        response = client.delete("/schemas/mysql/test_schema/versions/1")
        assert response.status_code == 200
        data = response.json()
        assert data["deprecated"] is True


@pytest.mark.unit
def test_list_schemas(client, mock_schema_service):
    """Test listing schemas."""
    with patch("data_manager.api.routes.schemas.get_schema_service", return_value=mock_schema_service):
        response = client.get("/schemas?database=mysql&page=1&page_size=10")
        assert response.status_code == 200
        data = response.json()
        assert "schemas" in data
        assert "total_count" in data


@pytest.mark.unit
def test_validate_data(client, mock_schema_service):
    """Test data validation."""
    with patch("data_manager.api.routes.schemas.get_schema_service", return_value=mock_schema_service):
        request = {
            "database": "mysql",
            "schema_name": "test_schema",
            "data": {"id": 1}
        }
        response = client.post("/schemas/validate", json=request)
        assert response.status_code == 200
        data = response.json()
        assert "valid" in data


@pytest.mark.unit
def test_check_compatibility(client, mock_schema_service):
    """Test schema compatibility check."""
    with patch("data_manager.api.routes.schemas.get_schema_service", return_value=mock_schema_service):
        request = {
            "database": "mysql",
            "schema_name": "test_schema",
            "version1": 1,
            "version2": 2
        }
        response = client.post("/schemas/compatibility", json=request)
        assert response.status_code == 200
        data = response.json()
        assert "compatible" in data


@pytest.mark.unit
def test_search_schemas(client, mock_schema_service):
    """Test searching schemas."""
    with patch("data_manager.api.routes.schemas.get_schema_service", return_value=mock_schema_service):
        response = client.get("/schemas/search?query=test")
        assert response.status_code == 200
        assert isinstance(response.json(), list)


@pytest.mark.unit
def test_bootstrap_schemas(client, mock_schema_service):
    """Test bootstrapping schemas."""
    with patch("data_manager.api.routes.schemas.get_schema_service", return_value=mock_schema_service):
        request = {
            "database": "mysql",
            "schemas": [{
                "name": "test_schema",
                "version": 1,
                "schema": {"type": "object"},
                "compatibility_mode": "BACKWARD"
            }],
            "overwrite_existing": False
        }
        response = client.post("/schemas/bootstrap", json=request)
        assert response.status_code == 200
        data = response.json()
        assert "registered_count" in data


@pytest.mark.unit
def test_get_cache_stats(client, mock_schema_service):
    """Test getting cache statistics."""
    with patch("data_manager.api.routes.schemas.get_schema_service", return_value=mock_schema_service):
        response = client.get("/schemas/cache/stats")
        assert response.status_code == 200
        data = response.json()
        assert "hits" in data


@pytest.mark.unit
def test_clear_cache(client, mock_schema_service):
    """Test clearing cache."""
    with patch("data_manager.api.routes.schemas.get_schema_service", return_value=mock_schema_service):
        response = client.post("/schemas/cache/clear")
        assert response.status_code == 200
        data = response.json()
        assert data["cleared"] is True


@pytest.mark.unit
def test_get_schema_service_no_db_manager(mock_db_manager):
    """Test get_schema_service when db_manager is not available."""
    import data_manager.api.routes.schemas as schemas_module
    original_db_manager = api_module.db_manager
    original_schema_service = schemas_module.schema_service

    try:
        api_module.db_manager = None
        schemas_module.schema_service = None
        with pytest.raises(Exception):
            schemas_module.get_schema_service()
    finally:
        api_module.db_manager = original_db_manager
        schemas_module.schema_service = original_schema_service

