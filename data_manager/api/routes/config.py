"""
Configuration management endpoints for TA Bot and other services.

Provides centralized configuration management through the data management service.
Includes auditing and rollback capabilities.
"""

import logging
import os
from datetime import datetime
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Path, Query, status
from pydantic import BaseModel, Field

from data_manager.db.database_manager import DatabaseManager
from data_manager.models.config import ConfigAudit

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


class RollbackRequest(BaseModel):
    """Request model for rolling back configuration."""

    target_version: int | None = Field(
        None, description="Specific version to rollback to"
    )
    rollback_id: str | None = Field(
        None, description="Specific audit record ID to rollback to"
    )
    changed_by: str = Field(..., description="Who is performing the rollback")
    reason: str | None = Field(None, description="Reason for rollback")


@router.get("/application", response_model=AppConfigResponse)
async def get_application_config():
    """
    Get application configuration.
    """
    if not db_manager or not db_manager.configuration:
        raise HTTPException(status_code=503, detail="Database manager not available")

    try:
        config = await db_manager.configuration.get_app_config()
        if not config:
            # Return defaults if no config found
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

        params = config.get("parameters", {})
        return AppConfigResponse(
            enabled_strategies=params.get("enabled_strategies", []),
            symbols=params.get("symbols", []),
            candle_periods=params.get("candle_periods", []),
            min_confidence=params.get("min_confidence", 0.6),
            max_confidence=params.get("max_confidence", 0.95),
            max_positions=params.get("max_positions", 10),
            position_sizes=params.get("position_sizes", [100, 200, 500, 1000]),
            version=config.get("version", 0),
            source="mongodb",
            created_at=config.get("created_at").isoformat()
            if isinstance(config.get("created_at"), datetime)
            else config.get("created_at", ""),
            updated_at=config.get("updated_at").isoformat()
            if isinstance(config.get("updated_at"), datetime)
            else config.get("updated_at", ""),
        )

    except Exception as e:
        logger.error(f"Error fetching application config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/application", response_model=AppConfigResponse)
async def update_application_config(request: AppConfigRequest):
    """
    Update application configuration with auditing.
    """
    if not db_manager or not db_manager.configuration:
        raise HTTPException(status_code=503, detail="Database manager not available")

    # Validate
    is_valid, errors = validate_application_config(request)
    if not is_valid:
        raise HTTPException(status_code=400, detail="; ".join(errors))

    if request.validate_only:
        return await get_application_config()  # Return current as placeholder

    try:
        parameters = {
            "enabled_strategies": request.enabled_strategies,
            "symbols": request.symbols,
            "candle_periods": request.candle_periods,
            "min_confidence": request.min_confidence,
            "max_confidence": request.max_confidence,
            "max_positions": request.max_positions,
            "position_sizes": request.position_sizes,
        }

        config = await db_manager.configuration.upsert_app_config(
            parameters=parameters, changed_by=request.changed_by, reason=request.reason
        )

        if not config:
            raise HTTPException(
                status_code=500, detail="Failed to update configuration"
            )

        return AppConfigResponse(
            **parameters,
            version=config["version"],
            source="mongodb",
            created_at=config["created_at"].isoformat()
            if isinstance(config["created_at"], datetime)
            else config["created_at"],
            updated_at=config["updated_at"].isoformat()
            if isinstance(config["updated_at"], datetime)
            else config["updated_at"],
        )
    except Exception as e:
        logger.error(f"Error updating application config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/strategies/{strategy_id}", response_model=StrategyConfigResponse)
async def get_strategy_config(
    strategy_id: str,
    symbol: str | None = Query(None, description="Symbol-specific configuration"),
    side: str | None = Query(None, description="Side-specific configuration"),
):
    """Get strategy configuration."""
    if not db_manager or not db_manager.configuration:
        raise HTTPException(status_code=503, detail="Database manager not available")

    try:
        config = await db_manager.configuration.get_strategy_config(
            strategy_id, symbol, side
        )
        if not config:
            return StrategyConfigResponse(
                parameters={},
                version=0,
                source="none",
                is_override=bool(symbol or side),
                created_at=datetime.utcnow().isoformat(),
                updated_at=datetime.utcnow().isoformat(),
            )

        return StrategyConfigResponse(
            parameters=config.get("parameters", {}),
            version=config.get("version", 0),
            source="mongodb",
            is_override=bool(symbol or side),
            created_at=config.get("created_at").isoformat()
            if isinstance(config.get("created_at"), datetime)
            else config.get("created_at", ""),
            updated_at=config.get("updated_at").isoformat()
            if isinstance(config.get("updated_at"), datetime)
            else config.get("updated_at", ""),
        )
    except Exception as e:
        logger.error(f"Error fetching strategy config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/strategies/{strategy_id}", response_model=StrategyConfigResponse)
async def update_strategy_config(
    strategy_id: str,
    request: StrategyConfigRequest,
    symbol: str | None = Query(None, description="Symbol-specific configuration"),
    side: str | None = Query(None, description="Side-specific configuration"),
):
    """Update strategy configuration with auditing."""
    if not db_manager or not db_manager.configuration:
        raise HTTPException(status_code=503, detail="Database manager not available")

    if not request.parameters:
        raise HTTPException(status_code=400, detail="parameters cannot be empty")

    if request.validate_only:
        return await get_strategy_config(strategy_id, symbol, side)

    try:
        config = await db_manager.configuration.upsert_strategy_config(
            strategy_id=strategy_id,
            parameters=request.parameters,
            changed_by=request.changed_by,
            symbol=symbol,
            side=side,
            reason=request.reason,
        )

        if not config:
            raise HTTPException(
                status_code=500, detail="Failed to update configuration"
            )

        return StrategyConfigResponse(
            parameters=config["parameters"],
            version=config["version"],
            source="mongodb",
            is_override=bool(symbol or side),
            created_at=config["created_at"].isoformat()
            if isinstance(config["created_at"], datetime)
            else config["created_at"],
            updated_at=config["updated_at"].isoformat()
            if isinstance(config["updated_at"], datetime)
            else config["updated_at"],
        )
    except Exception as e:
        logger.error(f"Error updating strategy config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/strategies/{strategy_id}")
async def delete_strategy_config(
    strategy_id: str,
    symbol: str | None = Query(
        None, description="Symbol-specific configuration to delete"
    ),
    side: str | None = Query(None, description="Side-specific configuration to delete"),
):
    """
    Delete strategy configuration.
    """
    if not db_manager or not db_manager.configuration:
        raise HTTPException(status_code=503, detail="Database manager not available")

    try:
        if side:
            await db_manager.mongodb.db.strategy_configs.delete_one(
                {"strategy_id": strategy_id, "symbol": symbol, "side": side}
            )
        elif symbol:
            await db_manager.mongodb.db.strategy_configs.delete_one(
                {"strategy_id": strategy_id, "symbol": symbol, "side": None}
            )
        else:
            await db_manager.mongodb.db.strategy_configs.delete_one(
                {"strategy_id": strategy_id, "symbol": None, "side": None}
            )

        return {"message": "Configuration deleted successfully"}

    except Exception as e:
        logger.error(f"Error deleting strategy config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/strategies")
async def list_strategy_configs():
    """List all strategy configurations."""
    if not db_manager:
        raise HTTPException(status_code=503, detail="Database manager not available")
    strategy_ids = await db_manager.mongodb.list_all_strategy_ids()
    return {"strategy_ids": strategy_ids}


@router.post("/cache/refresh")
async def refresh_config_cache():
    """Force refresh configuration cache."""
    return {"message": "Cache refresh requested"}


@router.get("/audit/application", response_model=list[dict[str, Any]])
async def get_app_audit_trail(limit: int = Query(100, ge=1, le=1000)):
    """Get application configuration audit trail."""
    if not db_manager or not db_manager.configuration:
        raise HTTPException(status_code=503, detail="Database manager not available")
    return await db_manager.configuration.get_audit_trail("application", limit=limit)


@router.get("/audit/strategies/{strategy_id}", response_model=list[dict[str, Any]])
async def get_strategy_audit_trail(
    strategy_id: str,
    symbol: str | None = Query(None),
    side: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
):
    """Get strategy configuration audit trail."""
    if not db_manager or not db_manager.configuration:
        raise HTTPException(status_code=503, detail="Database manager not available")
    return await db_manager.configuration.get_audit_trail(
        "strategy", strategy_id=strategy_id, symbol=symbol, side=side, limit=limit
    )


@router.post("/rollback/application", response_model=AppConfigResponse)
async def rollback_app_config(request: RollbackRequest):
    """Rollback application configuration."""
    if not db_manager or not db_manager.configuration:
        raise HTTPException(status_code=503, detail="Database manager not available")

    success, error, config = await db_manager.configuration.rollback(
        config_type="application",
        changed_by=request.changed_by,
        target_version=request.target_version,
        reason=request.reason,
    )

    if not success:
        raise HTTPException(status_code=400, detail=error)

    return await get_application_config()


@router.post(
    "/rollback/strategies/{strategy_id}", response_model=StrategyConfigResponse
)
async def rollback_strategy_config(
    strategy_id: str,
    request: RollbackRequest,
    symbol: str | None = Query(None),
    side: str | None = Query(None),
):
    """Rollback strategy configuration."""
    if not db_manager or not db_manager.configuration:
        raise HTTPException(status_code=503, detail="Database manager not available")

    success, error, config = await db_manager.configuration.rollback(
        config_type="strategy",
        strategy_id=strategy_id,
        symbol=symbol,
        side=side,
        changed_by=request.changed_by,
        target_version=request.target_version,
        reason=request.reason,
    )

    if not success:
        raise HTTPException(status_code=400, detail=error)

    return await get_strategy_config(strategy_id, symbol, side)


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
    suggested_value: Any | None = Field(
        None, description="Suggested correct value if applicable"
    )


class CrossServiceConflict(BaseModel):
    """Cross-service configuration conflict."""

    service: str = Field(..., description="Service name with conflicting configuration")
    conflict_type: str = Field(
        ..., description="Type of conflict (e.g., 'PARAMETER_CONFLICT')"
    )
    description: str = Field(..., description="Description of the conflict")
    resolution: str = Field(..., description="Suggested resolution")


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


class ConfigValidationRequest(BaseModel):
    """Request model for configuration validation."""

    config_type: str = Field(
        ..., description="Configuration type: 'application' or 'strategy'"
    )
    parameters: dict[str, Any] = Field(
        ..., description="Configuration parameters to validate"
    )
    strategy_id: str | None = Field(
        None,
        description="Strategy identifier (required for strategy config validation)",
    )
    symbol: str | None = Field(
        None, description="Trading symbol (optional, for symbol-specific validation)"
    )
    side: str | None = Field(None, description="Side-specific validation")


# Service URLs for cross-service conflict detection
SERVICE_URLS = {
    "tradeengine": os.getenv(
        "TRADEENGINE_URL", "http://petrosa-tradeengine-service:80"
    ),
    "ta-bot": os.getenv("TA_BOT_URL", "http://petrosa-ta-bot-service:80"),
    "realtime-strategies": os.getenv(
        "REALTIME_STRATEGIES_URL", "http://petrosa-realtime-strategies:80"
    ),
}


async def detect_cross_service_conflicts(
    config_type: str,
    parameters: dict[str, Any],
    strategy_id: str | None = None,
    symbol: str | None = None,
) -> list[CrossServiceConflict]:
    """Detect cross-service configuration conflicts."""
    conflicts = []
    timeout = httpx.Timeout(5.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        # Simplified conflict detection for now
        pass

    return conflicts


def validate_application_config(request: AppConfigRequest) -> tuple[bool, list[str]]:
    """Validate application configuration parameters."""
    errors = []
    if request.min_confidence >= request.max_confidence:
        errors.append(
            f"min_confidence ({request.min_confidence}) must be less than max_confidence ({request.max_confidence})"
        )
    if not request.enabled_strategies:
        errors.append("enabled_strategies cannot be empty")
    if not request.symbols:
        errors.append("symbols cannot be empty")
    return len(errors) == 0, errors


@router.post("/validate", response_model=dict[str, Any])
async def validate_config(request: ConfigValidationRequest):
    """Validate configuration without applying changes."""
    if not db_manager:
        raise HTTPException(status_code=503, detail="Database manager not available")

    try:
        validation_errors = []
        suggested_fixes = []

        if request.config_type == "application":
            # Basic validation for application config
            if (
                "min_confidence" in request.parameters
                and "max_confidence" in request.parameters
            ):
                if (
                    request.parameters["min_confidence"]
                    >= request.parameters["max_confidence"]
                ):
                    validation_errors.append(
                        ValidationError(
                            field="min_confidence",
                            message="min_confidence must be less than max_confidence",
                            code="VALIDATION_ERROR",
                        )
                    )

        validation_response = ValidationResponse(
            validation_passed=len(validation_errors) == 0,
            errors=validation_errors,
            warnings=[],
            suggested_fixes=suggested_fixes,
            estimated_impact={"risk_level": "low"},
            conflicts=[],
        )

        return {
            "success": True,
            "data": validation_response,
            "metadata": {"validation_mode": "dry_run"},
        }

    except Exception as e:
        logger.error(f"Error validating config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# --- Configuration Rollback Proxy Endpoints ---


@router.post("/{service}/rollback", summary="Proxy configuration rollback to service")
async def proxy_rollback(
    service: str,
    request: RollbackRequest,
    strategy_id: str | None = Query(None),
    symbol: str | None = Query(None),
    side: str | None = Query(None),
):
    """
    Proxy configuration rollback to a specific service.

    Supported services: ta-bot, realtime-strategies, tradeengine
    """
    if service not in SERVICE_URLS:
        raise HTTPException(status_code=400, detail=f"Unknown service: {service}")

    base_url = SERVICE_URLS[service]

    # Map service to its specific rollback endpoint
    if service == "ta-bot":
        if strategy_id:
            url = f"{base_url}/api/v1/strategies/{strategy_id}/rollback"
        else:
            url = f"{base_url}/api/v1/config/application/rollback"
    elif service == "tradeengine":
        url = f"{base_url}/api/v1/config/rollback"
    elif service == "realtime-strategies":
        if not strategy_id:
            raise HTTPException(
                status_code=400,
                detail="strategy_id is required for realtime-strategies",
            )
        url = f"{base_url}/api/v1/strategies/{strategy_id}/rollback"
    else:
        raise HTTPException(
            status_code=400, detail=f"Proxy not implemented for: {service}"
        )

    # Prepare query parameters
    params = {}
    if symbol:
        params["symbol"] = symbol
    if side:
        params["side"] = side

    logger.info(f"Proxying rollback request to {service} at {url} (params: {params})")

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, json=request.model_dump(), params=params)

            if response.status_code >= 400:
                logger.error(
                    f"Proxy rollback to {service} failed with {response.status_code}: {response.text}"
                )
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Service {service} returned error: {response.text}",
                )

            logger.info(f"Proxy rollback to {service} successful")
            return response.json()

    except httpx.TimeoutException:
        logger.error(f"Timeout while proxying rollback to {service}")
        raise HTTPException(
            status_code=504, detail=f"Timeout connecting to {service} service"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error proxying rollback to {service}: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to proxy rollback to {service}: {str(e)}"
        )


@router.get("/{service}/history", summary="Proxy configuration history to service")
async def proxy_history(
    service: str,
    strategy_id: str | None = Query(None),
    symbol: str | None = Query(None),
    side: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
):
    """
    Proxy configuration history request to a specific service.

    Supported services: ta-bot, realtime-strategies, tradeengine
    """
    if service not in SERVICE_URLS:
        raise HTTPException(status_code=400, detail=f"Unknown service: {service}")

    base_url = SERVICE_URLS[service]

    # Map service to its specific history/audit endpoint
    if service == "ta-bot":
        if strategy_id:
            url = f"{base_url}/api/v1/strategies/{strategy_id}/audit"
        else:
            url = f"{base_url}/api/v1/config/application/audit"
    elif service == "tradeengine":
        url = f"{base_url}/api/v1/config/history"
    elif service == "realtime-strategies":
        if not strategy_id:
            raise HTTPException(
                status_code=400,
                detail="strategy_id is required for realtime-strategies",
            )
        url = f"{base_url}/api/v1/strategies/{strategy_id}/audit"
    else:
        raise HTTPException(
            status_code=400, detail=f"Proxy not implemented for: {service}"
        )

    # Prepare query parameters
    params = {"limit": limit}
    if symbol:
        params["symbol"] = symbol
    if side:
        params["side"] = side

    logger.info(f"Proxying history request to {service} at {url} (params: {params})")

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, params=params)

            if response.status_code >= 400:
                logger.error(
                    f"Proxy history to {service} failed with {response.status_code}: {response.text}"
                )
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Service {service} returned error: {response.text}",
                )

            logger.info(f"Proxy history to {service} successful")
            return response.json()

    except httpx.TimeoutException:
        logger.error(f"Timeout while proxying history to {service}")
        raise HTTPException(
            status_code=504, detail=f"Timeout connecting to {service} service"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error proxying history to {service}: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to proxy history to {service}: {str(e)}"
        )
