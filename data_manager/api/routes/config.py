"""
Configuration management endpoints for TA Bot and other services.

Provides centralized configuration management through the data management service.
"""

import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from data_manager.db.database_manager import DatabaseManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/config", tags=["Configuration"])

# Global database manager instance
db_manager: DatabaseManager | None = None


def set_database_manager(manager: DatabaseManager) -> None:
    """Set the database manager instance."""
    global db_manager
    db_manager = manager


# Pydantic models for request/response
class AppConfigRequest(BaseModel):
    """Application configuration request model."""

    enabled_strategies: list[str] = Field(
        ..., description="List of enabled strategy IDs"
    )
    symbols: list[str] = Field(..., description="List of trading symbols")
    candle_periods: list[str] = Field(..., description="List of timeframes")
    min_confidence: float = Field(
        0.6, ge=0.0, le=1.0, description="Minimum confidence threshold"
    )
    max_confidence: float = Field(
        0.95, ge=0.0, le=1.0, description="Maximum confidence threshold"
    )
    max_positions: int = Field(10, ge=1, description="Maximum concurrent positions")
    position_sizes: list[int] = Field(
        [100, 200, 500, 1000], description="Available position sizes"
    )
    changed_by: str = Field(..., description="Who is making the change")
    reason: str | None = Field(None, description="Reason for the change")
    validate_only: bool = Field(
        False, description="If true, only validate parameters without saving"
    )


class AppConfigResponse(BaseModel):
    """Application configuration response model."""

    enabled_strategies: list[str]
    symbols: list[str]
    candle_periods: list[str]
    min_confidence: float
    max_confidence: float
    max_positions: int
    position_sizes: list[int]
    version: int
    source: str
    created_at: str
    updated_at: str


class StrategyConfigRequest(BaseModel):
    """Strategy configuration request model."""

    parameters: dict[str, Any] = Field(..., description="Strategy parameters")
    changed_by: str = Field(..., description="Who is making the change")
    reason: str | None = Field(None, description="Reason for the change")
    validate_only: bool = Field(
        False, description="If true, only validate parameters without saving"
    )


class StrategyConfigResponse(BaseModel):
    """Strategy configuration response model."""

    parameters: dict[str, Any]
    version: int
    source: str
    is_override: bool
    created_at: str
    updated_at: str


@router.get("/application", response_model=AppConfigResponse)
async def get_application_config():
    """
    Get application configuration for TA Bot.

    Returns the current application-level configuration including:
    - Enabled strategies
    - Trading symbols
    - Timeframes
    - Confidence thresholds
    - Risk management settings
    """
    if not db_manager:
        raise HTTPException(status_code=503, detail="Database manager not available")

    try:
        # Try to get from MongoDB first
        config_doc = await db_manager.mongodb.get_app_config()
        if config_doc:
            return AppConfigResponse(
                enabled_strategies=config_doc.get("enabled_strategies", []),
                symbols=config_doc.get("symbols", []),
                candle_periods=config_doc.get("candle_periods", []),
                min_confidence=config_doc.get("min_confidence", 0.6),
                max_confidence=config_doc.get("max_confidence", 0.95),
                max_positions=config_doc.get("max_positions", 10),
                position_sizes=config_doc.get("position_sizes", [100, 200, 500, 1000]),
                version=config_doc.get("version", 0),
                source="mongodb",
                created_at=config_doc.get("created_at", ""),
                updated_at=config_doc.get("updated_at", ""),
            )

        # Try MySQL as fallback
        if db_manager.mysql:
            config_doc = await db_manager.mysql.get_app_config()
            if config_doc:
                return AppConfigResponse(
                    enabled_strategies=config_doc.get("enabled_strategies", []),
                    symbols=config_doc.get("symbols", []),
                    candle_periods=config_doc.get("candle_periods", []),
                    min_confidence=config_doc.get("min_confidence", 0.6),
                    max_confidence=config_doc.get("max_confidence", 0.95),
                    max_positions=config_doc.get("max_positions", 10),
                    position_sizes=config_doc.get(
                        "position_sizes", [100, 200, 500, 1000]
                    ),
                    version=config_doc.get("version", 0),
                    source="mysql",
                    created_at=config_doc.get("created_at", ""),
                    updated_at=config_doc.get("updated_at", ""),
                )

        # Return defaults if no config found
        logger.warning("No application configuration found in database, using defaults")
        return AppConfigResponse(
            enabled_strategies=[],
            symbols=[],
            candle_periods=[],
            min_confidence=0.6,
            max_confidence=0.95,
            max_positions=10,
            position_sizes=[100, 200, 500, 1000],
            version=0,
            source="default",
            created_at=datetime.utcnow().isoformat(),
            updated_at=datetime.utcnow().isoformat(),
        )

    except Exception as e:
        logger.error(f"Error fetching application config: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to fetch application config: {str(e)}"
        )


@router.post("/application", response_model=AppConfigResponse)
async def update_application_config(request: AppConfigRequest):
    """
    Update application configuration for TA Bot.

    Creates or updates the application-level configuration with validation.
    """
    if not db_manager:
        raise HTTPException(status_code=503, detail="Database manager not available")

    try:
        # Validate configuration
        is_valid, errors = validate_application_config(request)
        if not is_valid:
            if request.validate_only:
                return {
                    "success": True,
                    "data": None,
                    "metadata": {
                        "validation": "failed",
                        "errors": errors,
                        "message": "Parameters are invalid (validate_only=true)",
                    },
                }
            else:
                raise HTTPException(status_code=400, detail="; ".join(errors))

        # If validate_only, return early without saving
        if request.validate_only:
            return {
                "success": True,
                "data": None,
                "metadata": {
                    "validation": "passed",
                    "message": "Parameters are valid but not saved (validate_only=true)",
                },
            }

        # Prepare configuration document
        config_doc = {
            "enabled_strategies": request.enabled_strategies,
            "symbols": request.symbols,
            "candle_periods": request.candle_periods,
            "min_confidence": request.min_confidence,
            "max_confidence": request.max_confidence,
            "max_positions": request.max_positions,
            "position_sizes": request.position_sizes,
            "version": 1,  # Will be incremented by database
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
            "changed_by": request.changed_by,
            "reason": request.reason,
        }

        # Save to MongoDB (primary)
        config_id = await db_manager.mongodb.upsert_app_config(
            config_doc, {"changed_by": request.changed_by, "reason": request.reason}
        )

        if config_id:
            logger.info(f"Application config updated in MongoDB: {config_id}")

        # Save to MySQL (fallback)
        if db_manager.mysql:
            try:
                await db_manager.mysql.upsert_app_config(
                    config_doc,
                    {"changed_by": request.changed_by, "reason": request.reason},
                )
                logger.info("Application config updated in MySQL")
            except Exception as e:
                logger.warning(f"Failed to update MySQL config: {e}")

        # Return the updated configuration
        return AppConfigResponse(
            enabled_strategies=request.enabled_strategies,
            symbols=request.symbols,
            candle_periods=request.candle_periods,
            min_confidence=request.min_confidence,
            max_confidence=request.max_confidence,
            max_positions=request.max_positions,
            position_sizes=request.position_sizes,
            version=config_doc["version"],
            source="mongodb",
            created_at=config_doc["created_at"],
            updated_at=config_doc["updated_at"],
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating application config: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to update application config: {str(e)}"
        )


@router.get("/strategies/{strategy_id}", response_model=StrategyConfigResponse)
async def get_strategy_config(
    strategy_id: str,
    symbol: str | None = Query(None, description="Symbol-specific configuration"),
):
    """
    Get strategy configuration.

    Returns configuration for a specific strategy, optionally for a specific symbol.
    """
    if not db_manager:
        raise HTTPException(status_code=503, detail="Database manager not available")

    try:
        # Try to get symbol-specific config first
        if symbol and db_manager.mongodb:
            config_doc = await db_manager.mongodb.get_symbol_config(strategy_id, symbol)
            if config_doc:
                return StrategyConfigResponse(
                    parameters=config_doc.get("parameters", {}),
                    version=config_doc.get("version", 0),
                    source="mongodb",
                    is_override=True,
                    created_at=config_doc.get("created_at", ""),
                    updated_at=config_doc.get("updated_at", ""),
                )

        # Try global strategy config
        if db_manager.mongodb:
            config_doc = await db_manager.mongodb.get_global_config(strategy_id)
            if config_doc:
                return StrategyConfigResponse(
                    parameters=config_doc.get("parameters", {}),
                    version=config_doc.get("version", 0),
                    source="mongodb",
                    is_override=False,
                    created_at=config_doc.get("created_at", ""),
                    updated_at=config_doc.get("updated_at", ""),
                )

        # Return empty config if not found
        logger.warning(f"No configuration found for strategy: {strategy_id}")
        return StrategyConfigResponse(
            parameters={},
            version=0,
            source="none",
            is_override=False,
            created_at=datetime.utcnow().isoformat(),
            updated_at=datetime.utcnow().isoformat(),
        )

    except Exception as e:
        logger.error(f"Error fetching strategy config for {strategy_id}: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to fetch strategy config: {str(e)}"
        )


@router.post("/strategies/{strategy_id}", response_model=StrategyConfigResponse)
async def update_strategy_config(
    strategy_id: str,
    request: StrategyConfigRequest,
    symbol: str | None = Query(None, description="Symbol-specific configuration"),
):
    """
    Update strategy configuration.

    Creates or updates configuration for a specific strategy.
    """
    if not db_manager:
        raise HTTPException(status_code=503, detail="Database manager not available")

    try:
        # Basic validation for strategy config
        if not request.parameters:
            if request.validate_only:
                return {
                    "success": True,
                    "data": None,
                    "metadata": {
                        "validation": "failed",
                        "errors": ["parameters cannot be empty"],
                        "message": "Parameters are invalid (validate_only=true)",
                    },
                }
            else:
                raise HTTPException(
                    status_code=400, detail="parameters cannot be empty"
                )

        # If validate_only, return early without saving
        if request.validate_only:
            return {
                "success": True,
                "data": None,
                "metadata": {
                    "validation": "passed",
                    "message": "Parameters are valid but not saved (validate_only=true)",
                },
            }

        # Prepare configuration document
        config_doc = {
            "strategy_id": strategy_id,
            "parameters": request.parameters,
            "version": 1,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
            "changed_by": request.changed_by,
            "reason": request.reason,
        }

        if symbol:
            config_doc["symbol"] = symbol
            config_doc["is_override"] = True

        # Save to MongoDB
        if symbol:
            config_id = await db_manager.mongodb.upsert_symbol_config(
                strategy_id,
                symbol,
                request.parameters,
                {"changed_by": request.changed_by, "reason": request.reason},
            )
        else:
            config_id = await db_manager.mongodb.upsert_global_config(
                strategy_id,
                request.parameters,
                {"changed_by": request.changed_by, "reason": request.reason},
            )

        if config_id:
            logger.info(f"Strategy config updated: {strategy_id} (symbol: {symbol})")

        return StrategyConfigResponse(
            parameters=request.parameters,
            version=config_doc["version"],
            source="mongodb",
            is_override=bool(symbol),
            created_at=config_doc["created_at"],
            updated_at=config_doc["updated_at"],
        )

    except Exception as e:
        logger.error(f"Error updating strategy config for {strategy_id}: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to update strategy config: {str(e)}"
        )


@router.delete("/strategies/{strategy_id}")
async def delete_strategy_config(
    strategy_id: str,
    symbol: str | None = Query(
        None, description="Symbol-specific configuration to delete"
    ),
):
    """
    Delete strategy configuration.

    Removes configuration for a specific strategy.
    """
    if not db_manager:
        raise HTTPException(status_code=503, detail="Database manager not available")

    try:
        if symbol:
            # Delete symbol-specific config
            await db_manager.mongodb.delete_symbol_config(strategy_id, symbol)
            logger.info(f"Deleted symbol-specific config: {strategy_id} for {symbol}")
        else:
            # Delete global config
            await db_manager.mongodb.delete_global_config(strategy_id)
            logger.info(f"Deleted global config: {strategy_id}")

        return {"message": "Configuration deleted successfully"}

    except Exception as e:
        logger.error(f"Error deleting strategy config for {strategy_id}: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to delete strategy config: {str(e)}"
        )


@router.get("/strategies")
async def list_strategy_configs():
    """
    List all strategy configurations.

    Returns a list of all strategy IDs with configurations.
    """
    if not db_manager:
        raise HTTPException(status_code=503, detail="Database manager not available")

    try:
        strategy_ids = await db_manager.mongodb.list_all_strategy_ids()
        return {"strategy_ids": strategy_ids}

    except Exception as e:
        logger.error(f"Error listing strategy configs: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to list strategy configs: {str(e)}"
        )


@router.post("/cache/refresh")
async def refresh_config_cache():
    """
    Force refresh configuration cache.

    Clears all cached configurations to force reload from database.
    """
    # This would be implemented if we add caching to the data manager
    # For now, just return success
    return {"message": "Cache refresh requested (no caching implemented yet)"}


# -------------------------------------------------------------------------
# Configuration Validation Models
# -------------------------------------------------------------------------


class ValidationError(BaseModel):
    """Standardized validation error format."""

    field: str = Field(..., description="Parameter name that failed validation")
    message: str = Field(..., description="Human-readable error message")
    code: str = Field(
        ...,
        description="Error code (e.g., 'INVALID_TYPE', 'OUT_OF_RANGE', 'UNKNOWN_PARAMETER')",
    )
    suggested_value: Optional[Any] = Field(
        None, description="Suggested correct value if applicable"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "field": "min_confidence",
                "message": "min_confidence must be less than max_confidence",
                "code": "VALIDATION_ERROR",
                "suggested_value": 0.6,
            }
        }


class CrossServiceConflict(BaseModel):
    """Cross-service configuration conflict."""

    service: str = Field(..., description="Service name with conflicting configuration")
    conflict_type: str = Field(
        ..., description="Type of conflict (e.g., 'PARAMETER_CONFLICT')"
    )
    description: str = Field(..., description="Description of the conflict")
    resolution: str = Field(..., description="Suggested resolution")

    class Config:
        json_schema_extra = {
            "example": {
                "service": "tradeengine",
                "conflict_type": "PARAMETER_CONFLICT",
                "description": "Conflicting confidence threshold settings",
                "resolution": "Use consistent confidence values across all services",
            }
        }


class ValidationResponse(BaseModel):
    """Standardized validation response across all services."""

    validation_passed: bool = Field(
        ..., description="Whether validation passed without errors"
    )
    errors: list[ValidationError] = Field(
        default_factory=list, description="List of validation errors"
    )
    warnings: list[str] = Field(
        default_factory=list, description="Non-blocking warnings"
    )
    suggested_fixes: list[str] = Field(
        default_factory=list, description="Actionable suggestions to fix errors"
    )
    estimated_impact: dict[str, Any] = Field(
        default_factory=dict,
        description="Estimated impact of configuration changes",
    )
    conflicts: list[CrossServiceConflict] = Field(
        default_factory=list, description="Cross-service conflicts detected"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "validation_passed": True,
                "errors": [],
                "warnings": [],
                "suggested_fixes": [],
                "estimated_impact": {
                    "risk_level": "low",
                    "affected_scope": "application",
                    "parameter_count": 7,
                },
                "conflicts": [],
            }
        }


class ConfigValidationRequest(BaseModel):
    """Request model for configuration validation."""

    config_type: str = Field(
        ..., description="Configuration type: 'application' or 'strategy'"
    )
    parameters: dict[str, Any] = Field(
        ..., description="Configuration parameters to validate"
    )
    strategy_id: Optional[str] = Field(
        None,
        description="Strategy identifier (required for strategy config validation)",
    )
    symbol: Optional[str] = Field(
        None, description="Trading symbol (optional, for symbol-specific validation)"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "config_type": "application",
                "parameters": {
                    "enabled_strategies": ["rsi_extreme_reversal"],
                    "symbols": ["BTCUSDT"],
                    "min_confidence": 0.6,
                    "max_confidence": 0.95,
                },
            }
        }


# Service URLs for cross-service conflict detection
SERVICE_URLS = {
    "tradeengine": os.getenv("TRADEENGINE_URL", "http://petrosa-tradeengine:8080"),
    "ta-bot": os.getenv("TA_BOT_URL", "http://petrosa-ta-bot:8080"),
    "realtime-strategies": os.getenv(
        "REALTIME_STRATEGIES_URL", "http://petrosa-realtime-strategies:8080"
    ),
}


async def detect_cross_service_conflicts(
    config_type: str,
    parameters: dict[str, Any],
    strategy_id: Optional[str] = None,
    symbol: Optional[str] = None,
) -> list[CrossServiceConflict]:
    """
    Detect cross-service configuration conflicts.

    Queries other services' /api/v1/config/validate endpoints to check for
    conflicting configurations.

    Args:
        config_type: Type of configuration ('application' or 'strategy')
        parameters: Configuration parameters to check
        strategy_id: Strategy identifier (for strategy configs)
        symbol: Trading symbol (optional)

    Returns:
        List of CrossServiceConflict objects
    """
    conflicts = []
    timeout = httpx.Timeout(5.0)  # Short timeout for conflict checks

    async with httpx.AsyncClient(timeout=timeout) as client:
        # Check tradeengine for conflicts (if configuring trading parameters)
        if config_type == "application" and any(
            param in parameters
            for param in ["min_confidence", "max_confidence", "max_positions"]
        ):
            try:
                # Check if tradeengine has conflicting leverage or position limits
                # This is a simplified check - can be enhanced
                if "max_positions" in parameters:
                    # Query tradeengine's current config to check for conflicts
                    try:
                        response = await client.get(
                            f"{SERVICE_URLS['tradeengine']}/api/v1/config/config/limits/global",
                            timeout=5.0,
                        )
                        if response.status_code == 200:
                            data = response.json()
                            if data.get("success") and data.get("data"):
                                current_max = data["data"].get("max_position_size")
                                proposed_max = parameters.get("max_positions")
                                if current_max and proposed_max:
                                    # Check if there's a significant mismatch
                                    if (
                                        abs(current_max - proposed_max)
                                        > current_max * 0.2
                                    ):
                                        conflicts.append(
                                            CrossServiceConflict(
                                                service="tradeengine",
                                                conflict_type="PARAMETER_CONFLICT",
                                                description=(
                                                    f"Position limit mismatch: data-manager proposes "
                                                    f"{proposed_max} max positions, but tradeengine has "
                                                    f"max_position_size={current_max}"
                                                ),
                                                resolution=(
                                                    "Align max_positions in data-manager with "
                                                    "max_position_size in tradeengine"
                                                ),
                                            )
                                        )
                    except Exception as e:
                        logger.debug(f"Could not check tradeengine for conflicts: {e}")

            except Exception as e:
                logger.debug(f"Error checking tradeengine conflicts: {e}")

        # Check ta-bot and realtime-strategies for strategy config conflicts
        if config_type == "strategy" and strategy_id:
            for service_name, service_url in [
                ("ta-bot", SERVICE_URLS["ta-bot"]),
                ("realtime-strategies", SERVICE_URLS["realtime-strategies"]),
            ]:
                try:
                    # Query the service's validate endpoint with the same parameters
                    validation_request = {
                        "parameters": parameters,
                        "strategy_id": strategy_id,
                    }
                    if symbol:
                        validation_request["symbol"] = symbol

                    response = await client.post(
                        f"{service_url}/api/v1/config/validate",
                        json=validation_request,
                        timeout=5.0,
                    )

                    if response.status_code == 200:
                        data = response.json()
                        if data.get("success") and data.get("data"):
                            validation_data = data["data"]
                            # Check if the service reports conflicts or validation issues
                            if not validation_data.get("validation_passed", True):
                                errors = validation_data.get("errors", [])
                                if errors:
                                    conflicts.append(
                                        CrossServiceConflict(
                                            service=service_name,
                                            conflict_type="VALIDATION_CONFLICT",
                                            description=(
                                                f"{service_name} reports validation errors for "
                                                f"strategy {strategy_id}: "
                                                f"{', '.join([e.get('message', '') for e in errors[:2]])}"
                                            ),
                                            resolution=(
                                                f"Review {service_name} validation errors and "
                                                "ensure parameter compatibility"
                                            ),
                                        )
                                    )

                except httpx.TimeoutException:
                    logger.debug(f"Timeout checking {service_name} for conflicts")
                except Exception as e:
                    logger.debug(f"Error checking {service_name} conflicts: {e}")

    return conflicts


def validate_application_config(request: AppConfigRequest) -> tuple[bool, list[str]]:
    """
    Validate application configuration parameters.

    Returns:
        Tuple of (is_valid, list_of_errors)
    """
    errors = []

    if request.min_confidence >= request.max_confidence:
        errors.append(
            f"min_confidence ({request.min_confidence}) must be less than max_confidence ({request.max_confidence})"
        )

    if not request.enabled_strategies:
        errors.append("enabled_strategies cannot be empty")

    if not request.symbols:
        errors.append("symbols cannot be empty")

    if not request.candle_periods:
        errors.append("candle_periods cannot be empty")

    return len(errors) == 0, errors


@router.post("/validate", response_model=dict[str, Any])
async def validate_config(request: ConfigValidationRequest):
    """
    Validate configuration without applying changes.

    **For LLM Agents**: Validate configuration parameters without persisting changes.

    This endpoint performs comprehensive validation including:
    - Parameter type and constraint validation
    - Dependency validation
    - Cross-service conflict detection (future)
    - Impact assessment

    **Example Request**:
    ```json
    {
      "config_type": "application",
      "parameters": {
        "enabled_strategies": ["rsi_extreme_reversal"],
        "symbols": ["BTCUSDT"],
        "min_confidence": 0.6,
        "max_confidence": 0.95
      }
    }
    ```

    **Example Response**:
    ```json
    {
      "success": true,
      "data": {
        "validation_passed": true,
        "errors": [],
        "warnings": [],
        "suggested_fixes": [],
        "estimated_impact": {
          "risk_level": "low",
          "affected_scope": "application"
        },
        "conflicts": []
      }
    }
    ```
    """
    if not db_manager:
        raise HTTPException(status_code=503, detail="Database manager not available")

    try:
        validation_errors = []
        suggested_fixes = []

        if request.config_type == "application":
            # Validate application config
            try:
                # Create a temporary AppConfigRequest for validation
                app_config = AppConfigRequest(
                    enabled_strategies=request.parameters.get("enabled_strategies", []),
                    symbols=request.parameters.get("symbols", []),
                    candle_periods=request.parameters.get("candle_periods", []),
                    min_confidence=request.parameters.get("min_confidence", 0.6),
                    max_confidence=request.parameters.get("max_confidence", 0.95),
                    max_positions=request.parameters.get("max_positions", 10),
                    position_sizes=request.parameters.get(
                        "position_sizes", [100, 200, 500, 1000]
                    ),
                    changed_by="validation_api",
                    reason="Validation only",
                    validate_only=True,
                )

                is_valid, errors = validate_application_config(app_config)

                for error_msg in errors:
                    # Parse error message to extract field and details
                    if "min_confidence" in error_msg and "max_confidence" in error_msg:
                        field = "min_confidence"
                        code = "VALIDATION_ERROR"
                        suggested_fixes.append(
                            "Ensure min_confidence is less than max_confidence"
                        )
                        validation_errors.append(
                            ValidationError(
                                field=field,
                                message=error_msg,
                                code=code,
                                suggested_value=app_config.max_confidence - 0.1,
                            )
                        )
                    elif "cannot be empty" in error_msg:
                        field = error_msg.split(" cannot be empty")[0]
                        code = "VALIDATION_ERROR"
                        suggested_fixes.append(
                            f"Provide at least one value for {field}"
                        )
                        validation_errors.append(
                            ValidationError(
                                field=field,
                                message=error_msg,
                                code=code,
                                suggested_value=None,
                            )
                        )
                    else:
                        validation_errors.append(
                            ValidationError(
                                field="unknown",
                                message=error_msg,
                                code="VALIDATION_ERROR",
                                suggested_value=None,
                            )
                        )

            except Exception as e:
                # Pydantic validation errors
                error_msg = str(e)
                if "field required" in error_msg.lower():
                    field = error_msg.split("Field required")[0].strip()
                    code = "MISSING_FIELD"
                    suggested_fixes.append(f"Provide required field: {field}")
                else:
                    field = "unknown"
                    code = "VALIDATION_ERROR"
                validation_errors.append(
                    ValidationError(
                        field=field,
                        message=error_msg,
                        code=code,
                        suggested_value=None,
                    )
                )

        elif request.config_type == "strategy":
            # Validate strategy config
            if not request.strategy_id:
                validation_errors.append(
                    ValidationError(
                        field="strategy_id",
                        message="strategy_id is required for strategy config validation",
                        code="MISSING_FIELD",
                        suggested_value=None,
                    )
                )
            else:
                # Strategy config validation is more lenient - just check parameters exist
                if not request.parameters:
                    validation_errors.append(
                        ValidationError(
                            field="parameters",
                            message="parameters cannot be empty",
                            code="VALIDATION_ERROR",
                            suggested_value=None,
                        )
                    )

        else:
            validation_errors.append(
                ValidationError(
                    field="config_type",
                    message=f"Invalid config_type: {request.config_type}. Must be 'application' or 'strategy'",
                    code="INVALID_VALUE",
                    suggested_value="application",
                )
            )

        # Estimate impact
        estimated_impact = {
            "risk_level": "low",
            "affected_scope": (
                request.config_type
                if not request.strategy_id
                else f"{request.config_type}:{request.strategy_id}"
            ),
            "parameter_count": len(request.parameters),
        }

        # Add risk assessment based on parameters
        high_risk_params = ["min_confidence", "max_confidence", "max_positions"]
        if any(param in request.parameters for param in high_risk_params):
            estimated_impact["risk_level"] = "medium"

        # Cross-service conflict detection
        conflicts = await detect_cross_service_conflicts(
            request.config_type, request.parameters, request.strategy_id, request.symbol
        )

        validation_response = ValidationResponse(
            validation_passed=len(validation_errors) == 0,
            errors=validation_errors,
            warnings=[],
            suggested_fixes=suggested_fixes,
            estimated_impact=estimated_impact,
            conflicts=conflicts,
        )

        return {
            "success": True,
            "data": validation_response,
            "metadata": {
                "validation_mode": "dry_run",
                "scope": (
                    request.config_type
                    if not request.strategy_id
                    else f"{request.config_type}:{request.strategy_id}"
                ),
            },
        }

    except Exception as e:
        logger.error(f"Error validating config: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to validate configuration: {str(e)}"
        )
