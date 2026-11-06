"""GET /api/v1/analysis/ground-truth error mapping (dm#844)."""

from __future__ import annotations

import asyncio
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


def test_budget_scales_with_window_and_pairs() -> None:
    # 8 pairs x 2 calls x 5 spans = 80 calls at 48/min = 100 s, x1.5 + 30 s
    assert ground_truth.time_budget_seconds(30, 8) == pytest.approx(180.0)
    assert ground_truth.time_budget_seconds(7, 8) < ground_truth.time_budget_seconds(
        30, 8
    )
    assert (
        ground_truth.time_budget_seconds(180, 8) > ground_truth.BUDGET_CEILING_SECONDS
    )


async def test_window_over_the_ceiling_is_refused_without_any_work(monkeypatch) -> None:
    compute = AsyncMock()
    monkeypatch.setattr(ground_truth, "compute_ground_truth", compute)
    with pytest.raises(HTTPException) as err:
        await ground_truth.get_ground_truth(window_days=180)
    assert err.value.status_code == 504
    assert err.value.detail["error"] == "ground_truth_time_budget"
    assert err.value.detail["window_days"] == 180
    assert err.value.detail["hint"] == "use a smaller window"
    compute.assert_not_awaited()


async def test_compute_that_overruns_its_budget_is_cut_off_with_504(
    monkeypatch,
) -> None:
    """Injected budget: the computation never finishes; the request does, at once."""
    never = asyncio.Event()

    async def hangs(*_args, **_kwargs):
        await never.wait()

    monkeypatch.setattr(ground_truth, "compute_ground_truth", hangs)
    monkeypatch.setattr(ground_truth, "time_budget_seconds", lambda *_: 0.01)
    with pytest.raises(HTTPException) as err:
        await asyncio.wait_for(ground_truth.get_ground_truth(window_days=30), timeout=2)
    assert err.value.status_code == 504
    assert err.value.detail["error"] == "ground_truth_time_budget"
