from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from data_manager.api.routes import cio_state
from data_manager.api.routes.cio_state import CioPauseEntry
from data_manager.db.repositories.backfill_repository import BackfillRepository
from data_manager.db.repositories.cio_auto_resume_repository import (
    CioAutoResumeRepository,
)


def _entry():
    return CioPauseEntry(
        strategy_id="strategy-1",
        service="cio",
        status="paused",
        paused_at=1,
        last_unavailable_at=2,
        min_pause_seconds=30,
        flap_count=1,
        attempts=1,
        next_attempt_at=3,
    )


@pytest.mark.asyncio
async def test_cio_repo_accepts_database_objects_without_truth_testing():
    database = MagicMock()
    database.__bool__.side_effect = NotImplementedError("no bool")
    database.__getitem__.return_value = MagicMock()
    adapter = SimpleNamespace(db=database)
    repo = CioAutoResumeRepository(None, adapter)

    assert repo._collection() is database["cio_auto_resume_registry"]


@pytest.mark.asyncio
async def test_cio_route_accepts_adapter_with_database_object():
    manager = SimpleNamespace(
        mysql_adapter=MagicMock(),
        mongodb_adapter=SimpleNamespace(db=MagicMock()),
    )
    original = cio_state.api_module.db_manager
    cio_state.api_module.db_manager = manager
    try:
        result = cio_state._repo()
    finally:
        cio_state.api_module.db_manager = original

    assert isinstance(result, CioAutoResumeRepository)


@pytest.mark.asyncio
async def test_backfill_create_job_supports_adapter_model_dump_mode():
    mysql = MagicMock()

    def write(models, _collection):
        assert models[0].model_dump(mode="python")["job_id"] == "job-1"

    mysql.write.side_effect = write
    repo = BackfillRepository(mysql, None)

    assert await repo.create_job({"job_id": "job-1", "status": "pending"}) is True
