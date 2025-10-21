"""
Health check endpoints for Kubernetes probes and monitoring.
"""

import logging
from datetime import datetime

from fastapi import APIRouter, Query
from pydantic import BaseModel

import data_manager.api.app as api_module

logger = logging.getLogger(__name__)

router = APIRouter()


class HealthStatus(BaseModel):
    """Health status response."""

    status: str
    timestamp: datetime
    version: str


class ReadinessStatus(BaseModel):
    """Readiness status response."""

    ready: bool
    components: dict
    timestamp: datetime


class DataQualityResponse(BaseModel):
    """Data quality response."""

    pair: str
    period: str | None
    health: dict
    metadata: dict
    parameters: dict


@router.get("/liveness")
async def liveness() -> HealthStatus:
    """
    Kubernetes liveness probe endpoint.
    Returns OK if the service is alive.
    """
    return HealthStatus(
        status="ok",
        timestamp=datetime.utcnow(),
        version="1.0.0",
    )


@router.get("/readiness")
async def readiness() -> ReadinessStatus:
    """
    Kubernetes readiness probe endpoint.
    Returns ready status based on dependencies.
    """
    components = {
        "nats": "unknown",
        "mysql": "unknown",
        "mongodb": "unknown",
        "auditor": "healthy",
        "analytics": "healthy",
    }

    # Check database connectivity
    if api_module.db_manager:
        health = api_module.db_manager.health_check()
        components["mysql"] = "healthy" if health["mysql"]["connected"] else "unhealthy"
        components["mongodb"] = "healthy" if health["mongodb"]["connected"] else "unhealthy"
    else:
        components["mysql"] = "not_configured"
        components["mongodb"] = "not_configured"

    # NATS status would need to be passed from consumer (TODO)
    components["nats"] = "healthy"  # Assume healthy for now

    # Service is ready if databases are connected
    all_healthy = components["mysql"] in ["healthy", "not_configured"] and components[
        "mongodb"
    ] in ["healthy", "not_configured"]

    return ReadinessStatus(
        ready=all_healthy,
        components=components,
        timestamp=datetime.utcnow(),
    )


@router.get("/summary")
async def health_summary():
    """
    Overall system health status summary.
    """
    # TODO: Implement actual health summary aggregation
    return {
        "data": {
            "total_datasets": 0,
            "healthy_datasets": 0,
            "degraded_datasets": 0,
            "unhealthy_datasets": 0,
            "overall_score": 100.0,
        },
        "metadata": {
            "last_updated": datetime.utcnow().isoformat(),
            "source": "data-manager",
        },
        "parameters": {},
    }


@router.get("")
async def data_health(
    pair: str = Query(..., description="Trading pair symbol"),
    period: str | None = Query(None, description="Data period/timeframe"),
) -> DataQualityResponse:
    """
    Get data quality metrics for a specific pair and period.

    Returns completeness, freshness, gaps, and duplicates information.
    """
    # TODO: Implement actual health check from database
    return DataQualityResponse(
        pair=pair,
        period=period,
        health={
            "completeness": 99.9,
            "freshness_sec": 5,
            "gaps": 0,
            "duplicates": 0,
            "consistency_score": 100.0,
            "quality_score": 99.5,
        },
        metadata={
            "last_audit": datetime.utcnow().isoformat(),
            "data_source": "mongodb",
        },
        parameters={
            "pair": pair,
            "period": period,
        },
    )
