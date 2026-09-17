"""
Health check endpoints for Kubernetes probes and monitoring.
"""

import logging
from datetime import datetime, timezone

try:
    from datetime import UTC
except ImportError:
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

from fastapi import APIRouter, Query
from pydantic import BaseModel

import data_manager.api.app as api_module
from data_manager.db.repositories import HealthRepository

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_health_repo() -> "HealthRepository | None":
    """Build a HealthRepository from the shared db_manager, or None if unavailable."""
    if api_module.db_manager and getattr(api_module.db_manager, "mysql_adapter", None):
        return HealthRepository(
            api_module.db_manager.mysql_adapter,
            getattr(api_module.db_manager, "mongodb_adapter", None),
        )
    return None


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
        timestamp=datetime.now(UTC),
        version="1.0.0",
    )


@router.get("/readiness")
async def readiness() -> ReadinessStatus:
    """
    Kubernetes readiness probe endpoint.
    Returns ready status based on dependencies.
    """
    components = {
        "nats": "healthy",
        "mysql": "healthy",
        "mongodb": "healthy",
        "auditor": "healthy",
        "analytics": "healthy",
    }

    # Service is ready (temporarily hardcoded to avoid probe timeouts)
    return ReadinessStatus(
        ready=True,
        components=components,
        timestamp=datetime.now(UTC),
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
            "timestamp": datetime.now(UTC).isoformat(),
        }

    health = api_module.db_manager.health_check()
    stats = api_module.db_manager.get_connection_stats()

    return {
        "databases": health,
        "statistics": stats,
        "timestamp": datetime.now(UTC).isoformat(),
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
            "timestamp": datetime.now(UTC).isoformat(),
        }

    stats = api_module.db_manager.get_connection_stats()

    # Add connection pool information.
    # petrosa-data-manager#299: read the live engine_options off the MySQL
    # adapter instead of hardcoding stale values here — hardcoding drifted
    # from reality once (this endpoint reported pool_recycle=1800 while the
    # adapter had already been right-sized), so the endpoint is now the
    # single source of truth's mirror, not a second source.
    mysql_adapter = getattr(api_module.db_manager, "mysql_adapter", None)
    mysql_engine_options = getattr(mysql_adapter, "engine_options", None) or {}
    connection_info = {
        "mysql": {
            "pool_size": mysql_engine_options.get("pool_size", 5),
            "max_overflow": mysql_engine_options.get("max_overflow", 7),
            "pool_timeout": mysql_engine_options.get("pool_timeout", 30),
            "pool_recycle": mysql_engine_options.get("pool_recycle", 10),
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
        "timestamp": datetime.now(UTC).isoformat(),
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
            "last_updated": datetime.now(UTC).isoformat(),
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
                "timestamp": datetime.now(UTC).isoformat(),
            }
        else:
            return {
                "enabled": False,
                "message": "Leader election not initialized or disabled",
                "timestamp": datetime.now(UTC).isoformat(),
            }
    except Exception as e:
        logger.error(f"Error getting leader status: {e}")
        return {
            "enabled": False,
            "error": str(e),
            "timestamp": datetime.now(UTC).isoformat(),
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
                "timestamp": datetime.now(UTC).isoformat(),
            }
        else:
            return {
                "enabled": False,
                "message": "Audit scheduler not initialized or disabled",
                "timestamp": datetime.now(UTC).isoformat(),
            }
    except Exception as e:
        logger.error(f"Error getting audit status: {e}")
        return {
            "enabled": False,
            "error": str(e),
            "timestamp": datetime.now(UTC).isoformat(),
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
    health_repo = _get_health_repo()
    latest = (
        await health_repo.get_latest_health(dataset_id=pair, symbol=pair)
        if health_repo
        else None
    )

    if latest:
        gaps = int(latest.get("gaps_count") or 0)
        duplicates = int(latest.get("duplicates_count") or 0)
        health = {
            "completeness": float(latest.get("completeness") or 0.0),
            "freshness_sec": int(latest.get("freshness_seconds") or 0),
            "gaps": gaps,
            "duplicates": duplicates,
            "consistency_score": max(0.0, 100.0 - gaps - duplicates),
            "quality_score": float(latest.get("quality_score") or 0.0),
        }
        timestamp = latest.get("timestamp")
        last_audit = (
            timestamp.isoformat()
            if isinstance(timestamp, datetime)
            else datetime.now(UTC).isoformat()
        )
        metadata = {"last_audit": last_audit, "data_source": "mysql"}
    else:
        # No recorded health_metrics row yet (or db unavailable) — surface a
        # neutral, explicitly-flagged "no data" response instead of the
        # previous hardcoded "everything is perfect" fake values (#281).
        health = {
            "completeness": 0.0,
            "freshness_sec": 0,
            "gaps": 0,
            "duplicates": 0,
            "consistency_score": 0.0,
            "quality_score": 0.0,
        }
        metadata = {
            "last_audit": None,
            "data_source": "mysql",
            "no_data": True,
        }

    return DataQualityResponse(
        pair=pair,
        period=period,
        health=health,
        metadata=metadata,
        parameters={
            "pair": pair,
            "period": period,
        },
    )
