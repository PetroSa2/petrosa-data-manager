"""GET /api/v1/analysis/ground-truth error mapping (dm#844)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from data_manager.api.routes import ground_truth
from data_manager.services.exchange_ground_truth_inputs import GroundTruthInputError
from data_manager.services.exchange_history_client import ExchangeHistoryError

pytestmark = pytest.mark.asyncio


async def test_returns_the_report(monkeypatch) -> None:
    compute = AsyncMock(return_value={"strategies": {}, "totals": {}})
    monkeypatch.setattr(ground_truth, "compute_ground_truth", compute)
    assert await ground_truth.get_ground_truth(window_days=14) == {
        "strategies": {},
        "totals": {},
    }
    assert compute.await_args.args[1] == 14


async def test_exchange_history_failure_is_502_with_the_route_error(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        ground_truth,
        "compute_ground_truth",
        AsyncMock(side_effect=ExchangeHistoryError(401, "unauthorized")),
    )
    with pytest.raises(HTTPException) as err:
        await ground_truth.get_ground_truth(window_days=30)
    assert err.value.status_code == 502
    assert err.value.detail == {
        "error": "exchange_history_unavailable",
        "reason": "unauthorized",
    }


async def test_missing_input_is_503(monkeypatch) -> None:
    monkeypatch.setattr(
        ground_truth,
        "compute_ground_truth",
        AsyncMock(
            side_effect=GroundTruthInputError("MongoDB and MySQL are both required")
        ),
    )
    with pytest.raises(HTTPException) as err:
        await ground_truth.get_ground_truth(window_days=30)
    assert err.value.status_code == 503
