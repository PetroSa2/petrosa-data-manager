from unittest.mock import AsyncMock

import pytest

from data_manager.backfiller.orchestrator import BackfillOrchestrator


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unsupported_data_type_marks_job_failed():
    orchestrator = BackfillOrchestrator.__new__(BackfillOrchestrator)
    orchestrator.backfill_repo = AsyncMock()
    orchestrator.backfill_repo.get_job.return_value = {
        "symbol": "BTCUSDT",
        "data_type": "legacy",
        "timeframe": "1h",
        "start_time": None,
        "end_time": None,
    }

    await orchestrator.execute_backfill("job-1")

    orchestrator.backfill_repo.update_status.assert_any_await("job-1", "running")
    orchestrator.backfill_repo.update_status.assert_any_await(
        "job-1", "failed", error="Unsupported data type: legacy"
    )
