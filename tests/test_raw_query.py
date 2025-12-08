"""
Tests for raw query API endpoints.
"""

import pytest
from unittest.mock import AsyncMock, Mock, patch
from fastapi.testclient import TestClient

import constants
import data_manager.api.app as api_module


@pytest.fixture
def client(mock_db_manager):
    """Create test client with mocked database."""
    app = api_module.create_app()
    api_module.db_manager = mock_db_manager
    yield TestClient(app)
    api_module.db_manager = None


@pytest.mark.unit
@patch.object(constants, "RAW_QUERY_ENABLED", True)
def test_execute_mysql_query_success(client, mock_mysql_adapter):
    """Test successful MySQL query execution."""
    mock_mysql_adapter.query = AsyncMock(return_value=[{"id": 1, "name": "test"}])
    
    request = {
        "query": "SELECT * FROM test_table LIMIT 10",
        "parameters": None,
        "timeout": 30
    }
    
    response = client.post("/api/v1/raw/mysql", json=request)
    assert response.status_code == 200
    data = response.json()
    assert "data" in data
    assert "metadata" in data
    assert data["metadata"]["database"] == "mysql"


@pytest.mark.unit
@patch.object(constants, "RAW_QUERY_ENABLED", False)
def test_execute_mysql_query_disabled(client):
    """Test MySQL query when raw queries are disabled."""
    request = {"query": "SELECT * FROM test_table"}
    response = client.post("/api/v1/raw/mysql", json=request)
    assert response.status_code == 403
    assert "disabled" in response.json()["detail"].lower()


@pytest.mark.unit
@patch.object(constants, "RAW_QUERY_ENABLED", True)
def test_execute_mysql_query_dangerous_operation(client):
    """Test MySQL query with dangerous operation."""
    request = {"query": "DROP TABLE test_table"}
    response = client.post("/api/v1/raw/mysql", json=request)
    assert response.status_code == 400
    assert "dangerous" in response.json()["detail"].lower()


@pytest.mark.unit
@patch.object(constants, "RAW_QUERY_ENABLED", True)
def test_execute_mysql_query_system_database(client):
    """Test MySQL query accessing system database."""
    request = {"query": "SELECT * FROM mysql.user"}
    response = client.post("/api/v1/raw/mysql", json=request)
    assert response.status_code == 400
    assert "system database" in response.json()["detail"].lower()


@pytest.mark.unit
@patch.object(constants, "RAW_QUERY_ENABLED", True)
def test_execute_mongodb_query_success(client, mock_mongodb_adapter):
    """Test successful MongoDB query execution."""
    mock_mongodb_adapter.find = AsyncMock(return_value=[{"_id": "123", "name": "test"}])
    
    request = {
        "query": '{"collection": "test_collection", "filter": {}}',
        "parameters": None
    }
    
    response = client.post("/api/v1/raw/mongodb", json=request)
    assert response.status_code == 200
    data = response.json()
    assert "data" in data
    assert "metadata" in data
    assert data["metadata"]["database"] == "mongodb"


@pytest.mark.unit
@patch.object(constants, "RAW_QUERY_ENABLED", False)
def test_execute_mongodb_query_disabled(client):
    """Test MongoDB query when raw queries are disabled."""
    request = {"query": '{"collection": "test"}'}
    response = client.post("/api/v1/raw/mongodb", json=request)
    assert response.status_code == 403
    assert "disabled" in response.json()["detail"].lower()


@pytest.mark.unit
@patch.object(constants, "RAW_QUERY_ENABLED", True)
def test_execute_mongodb_query_system_collection(client):
    """Test MongoDB query accessing system collection."""
    request = {"query": '{"collection": "system.users"}'}
    response = client.post("/api/v1/raw/mongodb", json=request)
    assert response.status_code == 400
    assert "system collection" in response.json()["detail"].lower()


@pytest.mark.unit
@patch.object(constants, "RAW_QUERY_ENABLED", True)
def test_execute_mysql_query_no_adapter(client):
    """Test MySQL query when adapter is not available."""
    api_module.db_manager.mysql_adapter = None
    request = {"query": "SELECT * FROM test_table"}
    response = client.post("/api/v1/raw/mysql", json=request)
    assert response.status_code == 503
    api_module.db_manager.mysql_adapter = Mock()  # Restore


@pytest.mark.unit
@patch.object(constants, "RAW_QUERY_ENABLED", True)
def test_execute_mongodb_query_no_adapter(client):
    """Test MongoDB query when adapter is not available."""
    api_module.db_manager.mongodb_adapter = None
    request = {"query": '{"collection": "test"}'}
    response = client.post("/api/v1/raw/mongodb", json=request)
    assert response.status_code == 503
    api_module.db_manager.mongodb_adapter = Mock()  # Restore


@pytest.mark.unit
@patch.object(constants, "RAW_QUERY_ENABLED", True)
def test_execute_mysql_query_error_handling(client, mock_mysql_adapter):
    """Test MySQL query error handling."""
    mock_mysql_adapter.query = AsyncMock(side_effect=Exception("Database error"))
    
    request = {"query": "SELECT * FROM test_table"}
    response = client.post("/api/v1/raw/mysql", json=request)
    assert response.status_code == 500


@pytest.mark.unit
@patch.object(constants, "RAW_QUERY_ENABLED", True)
def test_execute_mysql_query_tracks_metrics(client, mock_mysql_adapter, mock_db_manager):
    """Test that MySQL queries track metrics."""
    mock_mysql_adapter.query = AsyncMock(return_value=[])
    mock_db_manager.increment_query_count = Mock()
    
    request = {"query": "SELECT * FROM test_table"}
    client.post("/api/v1/raw/mysql", json=request)
    
    assert mock_db_manager.increment_query_count.called

