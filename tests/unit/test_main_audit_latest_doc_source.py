"""Coverage for ``DataManagerApp._audit_latest_doc_source`` (#300).

The audit-persistence-staleness detector wired into the P2.5 AuditEvaluator
needs a newest-document lookup per collection. This method is extracted
(rather than an inline closure in ``start()``) so it is directly
unit-testable, matching the ``_run_mongo_data_size_loop`` pattern.
"""

from __future__ import annotations

import types
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from data_manager.main import DataManagerApp


def _app_with_adapter(find_filtered_result):
    app = DataManagerApp()
    adapter = types.SimpleNamespace(
        find_filtered=AsyncMock(return_value=find_filtered_result)
    )
    app.db_manager = types.SimpleNamespace(mongodb_adapter=adapter)
    return app


@pytest.mark.asyncio
async def test_returns_none_when_no_db_manager():
    app = DataManagerApp()
    app.db_manager = None
    assert await app._audit_latest_doc_source("cio_decisions") is None


@pytest.mark.asyncio
async def test_returns_none_when_no_mongodb_adapter():
    app = DataManagerApp()
    app.db_manager = types.SimpleNamespace(mongodb_adapter=None)
    assert await app._audit_latest_doc_source("cio_decisions") is None


@pytest.mark.asyncio
async def test_returns_none_when_collection_never_written():
    app = _app_with_adapter([])
    result = await app._audit_latest_doc_source("pnl_events")
    assert result is None
    app.db_manager.mongodb_adapter.find_filtered.assert_awaited_once_with(
        "pnl_events", limit=1, sort_field="received_at", sort_order=-1
    )


@pytest.mark.asyncio
async def test_returns_none_when_received_at_missing():
    app = _app_with_adapter([{"decision_id": "D1"}])
    assert await app._audit_latest_doc_source("cio_decisions") is None


@pytest.mark.asyncio
async def test_returns_aware_datetime_for_naive_received_at():
    naive = datetime(2026, 9, 1, 2, 2, 26)  # noqa: DTZ001 — simulating motor's naive default
    app = _app_with_adapter([{"received_at": naive}])
    result = await app._audit_latest_doc_source("cio_decisions")
    assert result == naive.replace(tzinfo=UTC)
    assert result.tzinfo is not None


@pytest.mark.asyncio
async def test_passes_through_already_aware_received_at():
    aware = datetime(2026, 9, 14, 7, 30, 25, tzinfo=UTC)
    app = _app_with_adapter([{"received_at": aware}])
    result = await app._audit_latest_doc_source("execution_events")
    assert result == aware


@pytest.mark.asyncio
async def test_returns_none_and_logs_on_adapter_error():
    app = DataManagerApp()
    adapter = types.SimpleNamespace(
        find_filtered=AsyncMock(side_effect=RuntimeError("mongo down"))
    )
    app.db_manager = types.SimpleNamespace(mongodb_adapter=adapter)
    result = await app._audit_latest_doc_source("pnl_events")
    assert result is None
