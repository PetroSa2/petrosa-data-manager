"""
Anomaly detection endpoints.
"""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

import data_manager.api.app as api_module

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/anomalies")
async def get_anomalies(
    pair: str = Query(..., description="Trading pair symbol"),
    severity: Optional[str] = Query(None, description="Filter by severity"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum number of anomalies"),
) -> dict:
    """
    Get detected anomalies for a symbol.

    Returns list of anomalies with timestamps, severity, and details.
    """
    if not api_module.db_manager or not api_module.db_manager.mysql_adapter:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        from data_manager.db.repositories import AuditRepository

        audit_repo = AuditRepository(
            api_module.db_manager.mysql_adapter,
            api_module.db_manager.mongodb_adapter,
        )

        # Query audit logs for anomalies
        logs = audit_repo.get_recent_logs(dataset_id=pair, limit=limit)

        # Filter by severity if provided
        if severity:
            logs = [log for log in logs if log.get("severity") == severity]

        # Filter for anomaly type audits
        anomalies = [
            log for log in logs if "anomaly" in log.get("details", "").lower()
            or "outlier" in log.get("details", "").lower()
        ]

        return {
            "pair": pair,
            "anomalies": anomalies,
            "total_count": len(anomalies),
            "severity_filter": severity,
            "timestamp": datetime.utcnow().isoformat(),
        }

    except Exception as e:
        logger.error(f"Error fetching anomalies: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/anomalies/detect")
async def trigger_anomaly_detection(
    pair: str = Query(..., description="Trading pair symbol"),
    timeframe: str = Query("1h", description="Timeframe"),
    method: str = Query("zscore", description="Detection method (zscore, mad, isolation_forest)"),
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
            log for log in logs
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

