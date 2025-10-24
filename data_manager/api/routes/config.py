"""
Configuration management endpoints for TA Bot and other services.

Provides centralized configuration management through the data management service.
"""

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from data_manager.db.database_manager import DatabaseManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/config", tags=["Configuration"])

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
        if request.min_confidence >= request.max_confidence:
            raise HTTPException(
                status_code=400,
                detail="min_confidence must be less than max_confidence",
            )

        if not request.enabled_strategies:
            raise HTTPException(
                status_code=400, detail="enabled_strategies cannot be empty"
            )

        if not request.symbols:
            raise HTTPException(status_code=400, detail="symbols cannot be empty")

        if not request.candle_periods:
            raise HTTPException(
                status_code=400, detail="candle_periods cannot be empty"
            )

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
