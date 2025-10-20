"""
Backfill management endpoints.
"""

import logging
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Body, Path, Query
from pydantic import BaseModel

import data_manager.api.app as api_module

logger = logging.getLogger(__name__)

router = APIRouter()

# Global orchestrator (will be set by main app)
backfill_orchestrator = None


class BackfillRequestBody(BaseModel):
    """Backfill request body."""

    symbol: str
    data_type: str
    timeframe: Optional[str] = None
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
    started_at: Optional[datetime]
    completed_at: Optional[datetime]


class BackfillJobListResponse(BaseModel):
    """List of backfill jobs."""

    jobs: List[BackfillJobResponse]
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
    status: Optional[str] = Query(None, description="Filter by status"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum number of jobs"),
) -> BackfillJobListResponse:
    """
    List backfill jobs.

    Returns list of backfill jobs with optional status filter.
    """
    # TODO: Implement actual job listing from database
    return BackfillJobListResponse(
        jobs=[],
        total_count=0,
    )


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

