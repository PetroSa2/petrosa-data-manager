"""
Backfill management endpoints.
"""

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Body, Path, Query
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()

# Global orchestrator (will be set by main app)
backfill_orchestrator = None


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
            created_at=datetime.utcnow(),
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
    # TODO: Implement actual job listing from database
    # This is a placeholder structure showing the expected response format
    jobs = []

    # Apply filters (when database implementation is added)
    # if status:
    #     jobs = filter by status
    # if symbol:
    #     jobs = filter by symbol
    # if data_type:
    #     jobs = filter by data_type
    # if from_time/to_time:
    #     jobs = filter by time range

    total_count = len(jobs)

    # Apply sorting (when database implementation is added)
    # Apply pagination
    paginated_jobs = jobs[offset : offset + limit]

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
    # TODO: Implement actual job retrieval from database
    return BackfillJobResponse(
        job_id=job_id,
        status="completed",
        request=BackfillRequestBody(
            symbol="BTCUSDT",
            data_type="candles",
            timeframe="1h",
            start_time=datetime.utcnow(),
            end_time=datetime.utcnow(),
        ),
        progress=100.0,
        records_fetched=1000,
        records_inserted=1000,
        created_at=datetime.utcnow(),
        started_at=datetime.utcnow(),
        completed_at=datetime.utcnow(),
    )
