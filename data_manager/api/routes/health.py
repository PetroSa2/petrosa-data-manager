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
        components["mongodb"] = (
            "healthy" if health["mongodb"]["connected"] else "unhealthy"
        )
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


@router.get("/databases")
async def database_health():
    """
    Detailed database connection health status.

    Returns individual database connection status with metrics.
    """
    if not api_module.db_manager:
        return {
            "error": "Database manager not available",
            "timestamp": datetime.utcnow().isoformat(),
        }

    health = api_module.db_manager.health_check()
    stats = api_module.db_manager.get_connection_stats()

    return {
        "databases": health,
        "statistics": stats,
        "timestamp": datetime.utcnow().isoformat(),
    }


@router.get("/connections")
async def connection_stats():
    """
    Database connection pool statistics.

    Returns detailed connection pool metrics and statistics.
    """
    if not api_module.db_manager:
        return {
            "error": "Database manager not available",
            "timestamp": datetime.utcnow().isoformat(),
        }

    stats = api_module.db_manager.get_connection_stats()

    # Add connection pool information
    connection_info = {
        "mysql": {
            "pool_size": 5,  # From MySQL adapter configuration
            "max_overflow": 10,
            "pool_timeout": 30,
            "pool_recycle": 1800,
        },
        "mongodb": {
            "max_pool_size": 100,
            "min_pool_size": 0,
            "max_idle_time_ms": 0,
        },
    }

    return {
        "statistics": stats,
        "pool_configuration": connection_info,
        "timestamp": datetime.utcnow().isoformat(),
    }


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


@router.get("/leader")
async def leader_status():
    """
    Get leader election status.

    Returns information about the current leader pod and this pod's status.
    """
    try:
        # Access leader election manager from main app

        # Try to get the app instance (if available)
        # This is a simple approach - in production you might use dependency injection
        leader_election = getattr(api_module, "leader_election", None)

        if leader_election:
            status = leader_election.get_status()
            return {
                "enabled": True,
                "pod_id": status["pod_id"],
                "is_leader": status["is_leader"],
                "leader_pod_id": status["leader_pod_id"],
                "running": status["running"],
                "heartbeat_interval": status["heartbeat_interval"],
                "election_timeout": status["election_timeout"],
                "timestamp": datetime.utcnow().isoformat(),
            }
        else:
            return {
                "enabled": False,
                "message": "Leader election not initialized or disabled",
                "timestamp": datetime.utcnow().isoformat(),
            }
    except Exception as e:
        logger.error(f"Error getting leader status: {e}")
        return {
            "enabled": False,
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat(),
        }


@router.get("/audit-status")
async def audit_status():
    """
    Get audit scheduler status.

    Returns information about the audit scheduler including last run time,
    leader status, and configuration.
    """
    try:
        # Access audit scheduler status
        audit_scheduler = getattr(api_module, "audit_scheduler", None)

        if audit_scheduler:
            status = audit_scheduler.get_status()
            return {
                "enabled": True,
                "running": status["running"],
                "last_audit_time": status.get("last_audit_time"),
                "is_leader": status.get("is_leader", False),
                "leader_pod_id": status.get("leader_pod_id"),
                "pod_id": status.get("pod_id"),
                "timestamp": datetime.utcnow().isoformat(),
            }
        else:
            return {
                "enabled": False,
                "message": "Audit scheduler not initialized or disabled",
                "timestamp": datetime.utcnow().isoformat(),
            }
    except Exception as e:
        logger.error(f"Error getting audit status: {e}")
        return {
            "enabled": False,
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat(),
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
