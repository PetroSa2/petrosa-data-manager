from decimal import Decimal
from types import SimpleNamespace

import pytest

import data_manager.api.app as api_module
from data_manager.api.routes.analysis import evaluate_scorecard
from data_manager.services.scorecard_service import calculate_scorecard


def _close(value: str, *, position_id: str = "p1", strategy_id: str = "alpha") -> dict:
    return {
        "event_type": "filled",
        "pnl": value,
        "fee": "0.10",
        "position_id": position_id,
        "strategy_id": strategy_id,
        "decision_id": f"d-{position_id}",
        "timestamp": "2026-10-01T12:00:00Z",
    }


def test_scorecard_metrics_and_top_one_exclusion() -> None:
    rows = [_close("207.68", position_id="big")] + [_close("-2", position_id=f"p{i}") for i in range(1, 5)]
    result = calculate_scorecard(rows, minimum_trades=1)
    group = result["groups"][0]
    assert group["n_trades"] == 5
    assert group["win_rate"] == "0.2"
    assert Decimal(group["expectancy_per_trade"]) > 0
    assert Decimal(group["expectancy_ex_top1"]) < 0


def test_scorecard_zero_close_group_is_null_safe() -> None:
    result = calculate_scorecard([], positions=[{"position_id": "open", "strategy_id": "alpha", "status": "open", "unrealized_pnl_usd": "3.5"}])
    group = result["groups"][0]
    assert group["n_trades"] == 0
    assert group["win_rate"] is None
    assert group["open_positions"][0]["position_id"] == "open"


def test_scorecard_splits_cio_modes() -> None:
    rows = [_close("1", position_id="a"), _close("-1", position_id="b")]
    rows[0]["decision_id"] = "one"
    rows[1]["decision_id"] = "two"
    decisions = [{"decision_id": "one", "source": "mode-a"}, {"decision_id": "two", "source": "mode-b"}]
    result = calculate_scorecard(rows, cio_decisions=decisions, group_by="cio_mode", minimum_trades=1)
    assert {group["group"] for group in result["groups"]} == {"mode-a", "mode-b"}


def test_scorecard_fees_and_funding_conserve() -> None:
    rows = [_close("10", position_id="p1")]
    result = calculate_scorecard(
        rows,
        funding_events=[{"position_id": "p1", "funding_fee": "1.25", "timestamp": "2026-10-01T08:00:00Z"}],
        minimum_trades=1,
    )
    group = result["groups"][0]
    assert group["net_pnl"] == "8.65"
    assert result["total_funding"] == "1.25"
    assert result["funding_unallocated"] == "0.00"


@pytest.mark.asyncio
async def test_evaluate_is_unconfigured_and_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    class Configuration:
        async def get_app_config(self) -> dict:
            return {}

    monkeypatch.setattr(
        api_module,
        "db_manager",
        SimpleNamespace(mongodb_adapter=object(), configuration=Configuration()),
    )
    result = await evaluate_scorecard()
    assert result == {"status": "unconfigured", "groups": [], "writes": 0}
