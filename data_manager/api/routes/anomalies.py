"""
Anomaly detection endpoints.
"""

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

import data_manager.api.app as api_module

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/anomalies")
async def get_anomalies(
    pair: str = Query(..., description="Trading pair symbol"),
    severity: str | None = Query(None, description="Filter by severity"),
    status: str | None = Query(
        None, description="Filter by status (new, acknowledged, resolved)"
    ),
    from_time: datetime | None = Query(
        None, alias="from", description="Start time for filtering"
    ),
    to_time: datetime | None = Query(
        None, alias="to", description="End time for filtering"
    ),
    limit: int = Query(
        100,
        ge=1,
        le=1000,
        description="Maximum number of anomalies (default: 100, max: 1000)",
    ),
    offset: int = Query(0, ge=0, description="Pagination offset (default: 0)"),
    sort_by: str = Query(
        "timestamp", description="Sort by field (timestamp, severity)"
    ),
    sort_order: str = Query("desc", description="Sort order (asc, desc)"),
) -> dict:
    """
    Get detected anomalies for a symbol with filtering and pagination.

    Returns list of anomalies with timestamps, severity, and details.
    Supports time-based filtering, status filtering, and pagination.
    """
    if not api_module.db_manager or not api_module.db_manager.mysql_adapter:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        from data_manager.db.repositories import AuditRepository

        audit_repo = AuditRepository(
            api_module.db_manager.mysql_adapter,
            api_module.db_manager.mongodb_adapter,
        )

        # Query audit logs for anomalies (get more than needed for filtering)
        logs = audit_repo.get_recent_logs(dataset_id=pair, limit=limit * 10)

        # Filter for anomaly type audits
        anomalies = [
            log
            for log in logs
            if "anomaly" in log.get("details", "").lower()
            or "outlier" in log.get("details", "").lower()
        ]

        # Apply filters
        if severity:
            anomalies = [a for a in anomalies if a.get("severity") == severity]

        if status:
            anomalies = [a for a in anomalies if a.get("status") == status]

        if from_time:
            anomalies = [
                a
                for a in anomalies
                if a.get("timestamp")
                and (
                    isinstance(a["timestamp"], datetime)
                    and a["timestamp"] >= from_time
                    or isinstance(a["timestamp"], str)
                    and datetime.fromisoformat(a["timestamp"].replace("Z", "+00:00"))
                    >= from_time
                )
            ]

        if to_time:
            anomalies = [
                a
                for a in anomalies
                if a.get("timestamp")
                and (
                    isinstance(a["timestamp"], datetime)
                    and a["timestamp"] <= to_time
                    or isinstance(a["timestamp"], str)
                    and datetime.fromisoformat(a["timestamp"].replace("Z", "+00:00"))
                    <= to_time
                )
            ]

        total_count = len(anomalies)

        # Apply sorting
        reverse = sort_order.lower() == "desc"
        try:
            if sort_by == "timestamp":
                anomalies.sort(
                    key=lambda x: (
                        x.get("timestamp", datetime.min)
                        if isinstance(x.get("timestamp"), datetime)
                        else datetime.fromisoformat(
                            x.get("timestamp", "1970-01-01").replace("Z", "+00:00")
                        )
                    ),
                    reverse=reverse,
                )
            elif sort_by == "severity":
                # Sort by severity level (assuming: critical > high > medium > low)
                severity_order = {"critical": 4, "high": 3, "medium": 2, "low": 1}
                anomalies.sort(
                    key=lambda x: severity_order.get(x.get("severity", "").lower(), 0),
                    reverse=reverse,
                )
        except (KeyError, ValueError, TypeError) as e:
            logger.warning(f"Could not sort by {sort_by}: {e}")

        # Apply pagination
        paginated_anomalies = anomalies[offset : offset + limit]

        return {
            "pair": pair,
            "data": paginated_anomalies,
            "pagination": {
                "total": total_count,
                "limit": limit,
                "offset": offset,
                "page": (offset // limit) + 1,
                "pages": (total_count + limit - 1) // limit if limit > 0 else 0,
                "has_next": offset + limit < total_count,
                "has_previous": offset > 0,
            },
            "filters_applied": {
                "severity": severity,
                "status": status,
                "from": from_time.isoformat() if from_time else None,
                "to": to_time.isoformat() if to_time else None,
            },
            "sort": {
                "by": sort_by,
                "order": sort_order,
            },
            "timestamp": datetime.utcnow().isoformat(),
        }

    except Exception as e:
        logger.error(f"Error fetching anomalies: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/anomalies/detect")
async def trigger_anomaly_detection(
    pair: str = Query(..., description="Trading pair symbol"),
    timeframe: str = Query("1h", description="Timeframe"),
    method: str = Query(
        "zscore", description="Detection method (zscore, mad, isolation_forest)"
    ),
) -> dict:
    """
    Trigger on-demand anomaly detection.

    Returns detected anomalies immediately.
    """
    if not api_module.db_manager:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        from data_manager.ml import ML_AVAILABLE, StatisticalAnomalyDetector

        # Use statistical detector
        detector = StatisticalAnomalyDetector(api_module.db_manager)
        anomalies = await detector.detect_anomalies(pair, timeframe, method=method)

        # Try ML detector if requested and available
        if method == "isolation_forest" and ML_AVAILABLE:
            from data_manager.ml import MLAnomalyDetector

            ml_detector = MLAnomalyDetector(api_module.db_manager)
            anomalies = await ml_detector.detect_price_anomalies(pair, timeframe)

        return {
            "pair": pair,
            "timeframe": timeframe,
            "method": method,
            "anomalies_detected": len(anomalies),
            "anomalies": anomalies,
            "timestamp": datetime.utcnow().isoformat(),
        }

    except Exception as e:
        logger.error(f"Error triggering anomaly detection: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/anomalies/summary")
async def anomaly_summary() -> dict:
    """
    Get summary of anomalies across all pairs.

    Returns counts by severity and symbol.
    """
    if not api_module.db_manager or not api_module.db_manager.mysql_adapter:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        from data_manager.db.repositories import AuditRepository

        audit_repo = AuditRepository(
            api_module.db_manager.mysql_adapter,
            api_module.db_manager.mongodb_adapter,
        )

        # Get recent audit logs
        logs = audit_repo.get_recent_logs(limit=1000)

        # Filter anomaly-related logs
        anomaly_logs = [
            log
            for log in logs
            if "anomaly" in log.get("details", "").lower()
            or "outlier" in log.get("details", "").lower()
        ]

        # Group by severity
        by_severity = {}
        for log in anomaly_logs:
            severity = log.get("severity", "unknown")
            by_severity[severity] = by_severity.get(severity, 0) + 1

        # Group by symbol
        by_symbol = {}
        for log in anomaly_logs:
            symbol = log.get("symbol", "unknown")
            by_symbol[symbol] = by_symbol.get(symbol, 0) + 1

        return {
            "total_anomalies": len(anomaly_logs),
            "by_severity": by_severity,
            "by_symbol": by_symbol,
            "timestamp": datetime.utcnow().isoformat(),
        }

    except Exception as e:
        logger.error(f"Error generating anomaly summary: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
