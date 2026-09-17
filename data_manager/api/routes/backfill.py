"""
Backfill management endpoints.
"""

import logging
import uuid
from datetime import datetime, timezone

try:
    from datetime import UTC
except ImportError:
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

from fastapi import APIRouter, Body, HTTPException, Path, Query
from pydantic import BaseModel

import data_manager.api.app as api_module
from data_manager.db.repositories import BackfillRepository

logger = logging.getLogger(__name__)

router = APIRouter()

# Global orchestrator (will be set by main app)
backfill_orchestrator = None


def _get_backfill_repo() -> "BackfillRepository | None":
    """Build a BackfillRepository from the shared db_manager, or None if unavailable."""
    if api_module.db_manager and getattr(api_module.db_manager, "mysql_adapter", None):
        return BackfillRepository(
            api_module.db_manager.mysql_adapter,
            getattr(api_module.db_manager, "mongodb_adapter", None),
        )
    return None


def _row_to_job_response(row: dict) -> "BackfillJobResponse":
    """Map a backfill_jobs DB row to the API response model."""
    return BackfillJobResponse(
        job_id=row["job_id"],
        status=row["status"],
        request=BackfillRequestBody(
            symbol=row["symbol"],
            data_type=row["data_type"],
            timeframe=row.get("timeframe"),
            start_time=row["start_time"],
            end_time=row["end_time"],
        ),
        progress=float(row.get("progress") or 0.0),
        records_fetched=int(row.get("records_fetched") or 0),
        records_inserted=int(row.get("records_inserted") or 0),
        created_at=row["created_at"],
        started_at=row.get("started_at"),
        completed_at=row.get("completed_at"),
    )


class BackfillRequestBody(BaseModel):
    """Backfill request body."""

    symbol: str
    data_type: str
    timeframe: str | None = None
    start_time: datetime
    end_time: datetime
    priority: int = 5


class BackfillJobResponse(BaseModel):
    """Backfill job response."""

    job_id: str
    status: str
    request: BackfillRequestBody
    progress: float
    records_fetched: int
    records_inserted: int
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class BackfillJobListResponse(BaseModel):
    """List of backfill jobs."""

    jobs: list[BackfillJobResponse]
    total_count: int


@router.post("/start")
async def start_backfill(
    request: BackfillRequestBody = Body(..., description="Backfill request details"),
) -> BackfillJobResponse:
    """
    Trigger a manual backfill job.

    Creates a new backfill job to fetch and restore missing data.
    """
    from data_manager.models.events import BackfillRequest

    # Convert request body to BackfillRequest model
    backfill_request = BackfillRequest(
        symbol=request.symbol,
        data_type=request.data_type,
        timeframe=request.timeframe,
        start_time=request.start_time,
        end_time=request.end_time,
        priority=request.priority,
    )

    # Create job via orchestrator
    if backfill_orchestrator:
        job = await backfill_orchestrator.create_backfill_job(backfill_request)
    else:
        # Fallback if orchestrator not available
        job_id = str(uuid.uuid4())
        logger.warning("Backfill orchestrator not available, creating placeholder job")
        return BackfillJobResponse(
            job_id=job_id,
            status="pending",
            request=request,
            progress=0.0,
            records_fetched=0,
            records_inserted=0,
            created_at=datetime.now(UTC),
            started_at=None,
            completed_at=None,
        )

    return BackfillJobResponse(
        job_id=job.job_id,
        status=job.status,
        request=request,
        progress=job.progress,
        records_fetched=job.records_fetched,
        records_inserted=job.records_inserted,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
    )


@router.get("/jobs")
async def list_backfill_jobs(
    status: str | None = Query(None, description="Filter by status"),
    symbol: str | None = Query(None, description="Filter by trading symbol"),
    data_type: str | None = Query(
        None, description="Filter by data type (candles, trades, depth, funding)"
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
        description="Maximum number of jobs (default: 100, max: 1000)",
    ),
    offset: int = Query(0, ge=0, description="Pagination offset (default: 0)"),
    sort_by: str = Query(
        "created_at", description="Sort by field (created_at, started_at, priority)"
    ),
    sort_order: str = Query("desc", description="Sort order (asc, desc)"),
) -> dict:
    """
    List backfill jobs with filtering and pagination.

    Returns list of backfill jobs with comprehensive filtering options
    including status, symbol, data type, and time range. Results are paginated and sortable.
    """
    backfill_repo = _get_backfill_repo()
    if backfill_repo:
        rows, total_count = backfill_repo.list_jobs(
            status=status,
            symbol=symbol,
            data_type=data_type,
            from_time=from_time,
            to_time=to_time,
            sort_by=sort_by,
            sort_order=sort_order,
            limit=limit,
            offset=offset,
        )
        paginated_jobs = [_row_to_job_response(row) for row in rows]
    else:
        logger.warning(
            "db_manager/mysql_adapter not available; returning empty job list"
        )
        paginated_jobs = []
        total_count = 0

    return {
        "data": paginated_jobs,
        "pagination": {
            "total": total_count,
            "limit": limit,
            "offset": offset,
            "page": (offset // limit) + 1 if limit > 0 else 1,
            "pages": (total_count + limit - 1) // limit if limit > 0 else 0,
            "has_next": offset + limit < total_count,
            "has_previous": offset > 0,
        },
        "filters_applied": {
            "status": status,
            "symbol": symbol,
            "data_type": data_type,
            "from": from_time.isoformat() if from_time else None,
            "to": to_time.isoformat() if to_time else None,
        },
        "sort": {
            "by": sort_by,
            "order": sort_order,
        },
    }


@router.get("/jobs/{job_id}")
async def get_backfill_job(
    job_id: str = Path(..., description="Job identifier"),
) -> BackfillJobResponse:
    """
    Get backfill job status and progress.

    Returns detailed information about a specific backfill job.
    """
    backfill_repo = _get_backfill_repo()
    if not backfill_repo:
        raise HTTPException(status_code=503, detail="Database not available")

    row = await backfill_repo.get_job(job_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Backfill job {job_id} not found")

    return _row_to_job_response(row)
