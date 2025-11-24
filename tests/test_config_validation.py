"""
Tests for configuration validation API endpoint.

Tests the /api/v1/config/validate endpoint including:
- Application config validation
- Strategy config validation
- Error format standardization
- Impact assessment
- Cross-service conflict detection
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import data_manager.api.app as api_module
from data_manager.api.routes.config import (
    CrossServiceConflict,
    ValidationError,
    ValidationResponse,
)


@pytest.fixture
def client(mock_db_manager):
    """Create test client with mocked database."""
    app = api_module.create_app()
    # Inject mock database manager into config routes
    from data_manager.api.routes import config as config_routes

    original_db_manager = config_routes.db_manager
    config_routes.db_manager = mock_db_manager
    yield TestClient(app)
    # Cleanup
    config_routes.db_manager = original_db_manager


class TestConfigValidationEndpoint:
    """Test /api/v1/config/validate endpoint."""

    @patch("data_manager.api.routes.config.detect_cross_service_conflicts")
    def test_validate_application_config_success(
        self, mock_detect_conflicts, client, mock_db_manager
    ):
        """Test successful validation for application config."""
        mock_detect_conflicts.return_value = []  # No conflicts

        response = client.post(
            "/api/v1/config/validate",
            json={
                "config_type": "application",
                "parameters": {
                    "enabled_strategies": ["rsi_extreme_reversal"],
                    "symbols": ["BTCUSDT", "ETHUSDT"],
                    "candle_periods": ["5m", "15m"],
                    "min_confidence": 0.6,
                    "max_confidence": 0.95,
                    "max_positions": 10,
                },
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["data"]["validation_passed"] is True
        assert len(data["data"]["errors"]) == 0
        assert data["data"]["estimated_impact"]["risk_level"] == "medium"
        assert data["data"]["estimated_impact"]["affected_scope"] == "application"
        assert data["metadata"]["validation_mode"] == "dry_run"
        mock_detect_conflicts.assert_called_once()

    @patch("data_manager.api.routes.config.detect_cross_service_conflicts")
    def test_validate_strategy_config_success(
        self, mock_detect_conflicts, client, mock_db_manager
    ):
        """Test successful validation for strategy config."""
        mock_detect_conflicts.return_value = []  # No conflicts

        response = client.post(
            "/api/v1/config/validate",
            json={
                "config_type": "strategy",
                "strategy_id": "rsi_extreme_reversal",
                "parameters": {
                    "rsi_period": 14,
                    "oversold_threshold": 30,
                },
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["data"]["validation_passed"] is True
        assert len(data["data"]["errors"]) == 0
        assert data["data"]["estimated_impact"]["risk_level"] == "low"
        assert (
            data["data"]["estimated_impact"]["affected_scope"]
            == "strategy:rsi_extreme_reversal"
        )
        mock_detect_conflicts.assert_called_once()

    def test_validate_application_config_min_confidence_error(self, client, mock_db_manager):
        """Test validation error for min_confidence > max_confidence."""
        response = client.post(
            "/api/v1/config/validate",
            json={
                "config_type": "application",
                "parameters": {
                    "enabled_strategies": ["rsi_extreme_reversal"],
                    "symbols": ["BTCUSDT"],
                    "candle_periods": ["5m"],
                    "min_confidence": 0.95,  # Greater than max_confidence
                    "max_confidence": 0.6,
                },
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["data"]["validation_passed"] is False
        assert len(data["data"]["errors"]) > 0
        assert any("min_confidence" in str(error) for error in data["data"]["errors"])

    def test_validate_application_config_empty_strategies(self, client, mock_db_manager):
        """Test validation error for empty enabled_strategies."""
        response = client.post(
            "/api/v1/config/validate",
            json={
                "config_type": "application",
                "parameters": {
                    "enabled_strategies": [],  # Empty
                    "symbols": ["BTCUSDT"],
                    "candle_periods": ["5m"],
                },
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["data"]["validation_passed"] is False
        assert len(data["data"]["errors"]) > 0
        assert any("cannot be empty" in str(error) for error in data["data"]["errors"])

    def test_validate_strategy_config_missing_strategy_id(self, client, mock_db_manager):
        """Test validation error when strategy_id is missing for strategy config."""
        response = client.post(
            "/api/v1/config/validate",
            json={
                "config_type": "strategy",
                # Missing strategy_id
                "parameters": {"rsi_period": 14},
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["data"]["validation_passed"] is False
        assert len(data["data"]["errors"]) > 0
        assert any("strategy_id" in str(error) for error in data["data"]["errors"])

    def test_validate_strategy_config_empty_parameters(self, client, mock_db_manager):
        """Test validation error for empty parameters."""
        response = client.post(
            "/api/v1/config/validate",
            json={
                "config_type": "strategy",
                "strategy_id": "rsi_extreme_reversal",
                "parameters": {},  # Empty
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["data"]["validation_passed"] is False
        assert len(data["data"]["errors"]) > 0
        assert any("cannot be empty" in str(error) for error in data["data"]["errors"])

    def test_validate_config_invalid_config_type(self, client, mock_db_manager):
        """Test validation error for invalid config_type."""
        response = client.post(
            "/api/v1/config/validate",
            json={
                "config_type": "invalid_type",  # Invalid
                "parameters": {"test": "value"},
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["data"]["validation_passed"] is False
        assert len(data["data"]["errors"]) > 0
        assert any("config_type" in str(error) for error in data["data"]["errors"])

    @patch("data_manager.api.routes.config.detect_cross_service_conflicts")
    def test_validate_config_with_conflicts(self, mock_detect_conflicts, client, mock_db_manager):
        """Test validation with cross-service conflicts."""
        mock_detect_conflicts.return_value = [
            CrossServiceConflict(
                service="tradeengine",
                conflict_type="PARAMETER_CONFLICT",
                description="Conflicting confidence threshold settings",
                resolution="Use consistent confidence values across all services",
            )
        ]

        response = client.post(
            "/api/v1/config/validate",
            json={
                "config_type": "application",
                "parameters": {
                    "enabled_strategies": ["rsi_extreme_reversal"],
                    "symbols": ["BTCUSDT"],
                    "candle_periods": ["5m"],
                    "min_confidence": 0.6,
                    "max_confidence": 0.95,
                },
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert len(data["data"]["conflicts"]) == 1
        assert data["data"]["conflicts"][0]["service"] == "tradeengine"

    def test_validate_config_risk_assessment_high_risk(self, client, mock_db_manager):
        """Test risk assessment for high-risk parameters."""
        response = client.post(
            "/api/v1/config/validate",
            json={
                "config_type": "application",
                "parameters": {
                    "enabled_strategies": ["rsi_extreme_reversal"],
                    "symbols": ["BTCUSDT"],
                    "candle_periods": ["5m"],
                    "min_confidence": 0.6,
                    "max_confidence": 0.95,
                    "max_positions": 20,  # High-risk parameter
                },
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["data"]["estimated_impact"]["risk_level"] == "medium"

    def test_validate_config_with_symbol(self, client, mock_db_manager):
        """Test validation with symbol parameter."""
        response = client.post(
            "/api/v1/config/validate",
            json={
                "config_type": "strategy",
                "strategy_id": "rsi_extreme_reversal",
                "symbol": "BTCUSDT",
                "parameters": {"rsi_period": 14},
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["data"]["validation_passed"] is True

    def test_validate_config_database_manager_unavailable(self, client):
        """Test validation when database manager is unavailable."""
        from data_manager.api.routes import config as config_routes

        # Set db_manager to None
        original_db_manager = config_routes.db_manager
        config_routes.db_manager = None

        try:
            response = client.post(
                "/api/v1/config/validate",
                json={
                    "config_type": "application",
                    "parameters": {"enabled_strategies": ["test"]},
                },
            )

            assert response.status_code == 503
        finally:
            # Restore db_manager
            config_routes.db_manager = original_db_manager

    def test_validate_config_pydantic_validation_error(self, client, mock_db_manager):
        """Test validation with Pydantic validation errors."""
        response = client.post(
            "/api/v1/config/validate",
            json={
                "config_type": "application",
                "parameters": {
                    "enabled_strategies": ["rsi_extreme_reversal"],
                    "symbols": ["BTCUSDT"],
                    "candle_periods": ["5m"],
                    "min_confidence": 1.5,  # Invalid: > 1.0
                    "max_confidence": 0.95,
                },
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["data"]["validation_passed"] is False
        assert len(data["data"]["errors"]) > 0

    @patch("data_manager.api.routes.config.detect_cross_service_conflicts")
    def test_validate_config_suggested_fixes(self, mock_detect_conflicts, client, mock_db_manager):
        """Test that suggested fixes are included in response."""
        mock_detect_conflicts.return_value = []

        response = client.post(
            "/api/v1/config/validate",
            json={
                "config_type": "application",
                "parameters": {
                    "enabled_strategies": [],  # Empty - should trigger suggested fix
                    "symbols": ["BTCUSDT"],
                    "candle_periods": ["5m"],
                },
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        # Should have suggested fixes even if validation fails
        assert "suggested_fixes" in data["data"]
        if not data["data"]["validation_passed"]:
            assert len(data["data"]["suggested_fixes"]) > 0

