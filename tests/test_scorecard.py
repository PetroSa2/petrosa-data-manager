from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import data_manager.api.app as api_module
import data_manager.api.routes.analysis as analysis_route
from data_manager.api.routes.analysis import evaluate_scorecard, get_scorecard
from data_manager.services.scorecard_service import (
    ScorecardService,
    calculate_scorecard,
    money,
    timestamp,
)


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
    rows = [_close("207.68", position_id="big")] + [
        _close("-2", position_id=f"p{i}") for i in range(1, 5)
    ]
    result = calculate_scorecard(rows, minimum_trades=1)
    group = result["groups"][0]
    assert group["n_trades"] == 5
    assert group["win_rate"] == "0.2"
    assert Decimal(group["expectancy_per_trade"]) > 0
    assert Decimal(group["expectancy_ex_top1"]) < 0


def test_scorecard_zero_close_group_is_null_safe() -> None:
    result = calculate_scorecard(
        [],
        positions=[
            {
                "position_id": "open",
                "strategy_id": "alpha",
                "status": "open",
                "unrealized_pnl_usd": "3.5",
            }
        ],
    )
    group = result["groups"][0]
    assert group["n_trades"] == 0
    assert group["win_rate"] is None
    assert group["open_positions"][0]["position_id"] == "open"


def test_scorecard_splits_cio_modes() -> None:
    rows = [_close("1", position_id="a"), _close("-1", position_id="b")]
    rows[0]["decision_id"] = "one"
    rows[1]["decision_id"] = "two"
    decisions = [
        {"decision_id": "one", "source": "mode-a"},
        {"decision_id": "two", "source": "mode-b"},
    ]
    result = calculate_scorecard(
        rows, cio_decisions=decisions, group_by="cio_mode", minimum_trades=1
    )
    assert {group["group"] for group in result["groups"]} == {"mode-a", "mode-b"}


def test_scorecard_fees_and_funding_conserve() -> None:
    rows = [_close("10", position_id="p1")]
    result = calculate_scorecard(
        rows,
        funding_events=[
            {
                "position_id": "p1",
                "funding_fee": "1.25",
                "timestamp": "2026-10-01T08:00:00Z",
            }
        ],
        minimum_trades=1,
    )
    group = result["groups"][0]
    assert group["net_pnl"] == "8.65"
    assert result["total_funding"] == "1.25"
    assert result["funding_unallocated"] == "0.00"


def test_scorecard_joins_audit_events_and_groups_by_strategy_symbol() -> None:
    result = calculate_scorecard(
        execution_events=[
            {"event_type": "filled", "position_id": "p1", "fee": "0.25"},
            {
                **_close("2", position_id="p1"),
                "symbol": "BTCUSDT",
                "fee": "0.10",
            },
        ],
        pnl_events=[
            {
                "pnl_kind": "closed",
                "position_id": "p2",
                "realized_pnl_usd": "3",
            }
        ],
        positions=[
            {"position_id": "closed", "status": "closed", "symbol": "ETHUSDT"},
            {
                "position_id": "open",
                "status": "open",
                "strategy_id": "beta",
                "symbol": "ETHUSDT",
                "unrealized_pnl_usd": "1.5",
            },
        ],
        group_by="strategy_symbol",
        minimum_trades=1,
    )

    groups = {group["group"]: group for group in result["groups"]}
    assert groups["alpha:BTCUSDT"]["net_pnl"] == "1.65"
    assert groups["beta:ETHUSDT"]["open_positions"][0]["position_id"] == "open"
    assert groups["unattributed:unknown"]["gross_pnl"] == "3"


def test_scorecard_normalizes_invalid_values_and_timestamps() -> None:
    naive = datetime(2026, 10, 1, 12, 0)

    assert money(None) == Decimal("0")
    assert money("not-a-number") == Decimal("0")
    assert timestamp({}) is None
    assert timestamp({"timestamp": naive}) == naive.replace(tzinfo=UTC)
    assert timestamp({"timestamp": "not-a-timestamp"}) is None


@pytest.mark.asyncio
async def test_scorecard_service_reads_all_audit_collections() -> None:
    class Mongo:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def find_filtered(self, collection: str, **kwargs: object) -> list[dict]:
            self.calls.append((collection, kwargs))
            return []

    mongodb = Mongo()
    start = datetime(2026, 10, 1, tzinfo=UTC)
    end = datetime(2026, 10, 2, tzinfo=UTC)

    result = await ScorecardService(mongodb).calculate(
        start=start,
        end=end,
        group_by="strategy",
        minimum_trades=1,
    )

    assert result["groups"] == []
    assert [collection for collection, _ in mongodb.calls] == [
        "execution_events",
        "pnl_events",
        "cio_decisions",
        "funding_rates",
        "positions",
    ]
    assert mongodb.calls[0][1] == {
        "start": start,
        "end": end,
        "limit": 10000,
        "sort_order": 1,
    }
    assert mongodb.calls[-1][1] == {"limit": 10000, "sort_order": 1}


@pytest.mark.asyncio
async def test_get_scorecard_validates_filters_and_maps_service_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(HTTPException) as invalid_group:
        await get_scorecard(group_by="invalid")
    assert invalid_group.value.status_code == 422

    with pytest.raises(HTTPException) as invalid_range:
        await get_scorecard(
            from_=datetime(2026, 10, 2, tzinfo=UTC),
            to=datetime(2026, 10, 1, tzinfo=UTC),
            group_by="strategy",
        )
    assert invalid_range.value.status_code == 422

    monkeypatch.setattr(api_module, "db_manager", None)
    with pytest.raises(HTTPException) as unavailable:
        await get_scorecard(from_=None, to=None, group_by="strategy", minimum_trades=30)
    assert unavailable.value.status_code == 503

    monkeypatch.setattr(
        api_module,
        "db_manager",
        SimpleNamespace(mongodb_adapter=object()),
    )
    calls: list[dict] = []

    async def calculate(self: ScorecardService, **kwargs: object) -> dict:
        calls.append(kwargs)
        return {"groups": [], "group_by": kwargs["group_by"]}

    monkeypatch.setattr(ScorecardService, "calculate", calculate)
    result = await get_scorecard(
        from_=None, to=None, group_by="cio_mode", minimum_trades=2
    )
    assert result == {"groups": [], "group_by": "cio_mode"}
    assert calls == [
        {"start": None, "end": None, "group_by": "cio_mode", "minimum_trades": 2}
    ]

    async def fail(self: ScorecardService, **kwargs: object) -> dict:
        raise RuntimeError("read failed")

    monkeypatch.setattr(ScorecardService, "calculate", fail)
    with pytest.raises(HTTPException) as failed:
        await get_scorecard(from_=None, to=None, group_by="strategy", minimum_trades=30)
    assert failed.value.status_code == 503


@pytest.mark.asyncio
async def test_evaluate_scorecard_reports_policy_statuses_and_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Configuration:
        async def get_app_config(self) -> dict:
            return {
                "parameters": {
                    "scorecard_min_trades": 12,
                    "scorecard_min_expectancy_net": "1.0",
                    "scorecard_max_dd_fraction": "0.2",
                }
            }

    scorecard = {
        "groups": [
            {
                "group": "watch",
                "sample_ok": False,
                "expectancy_per_trade": "9",
                "max_drawdown": "0",
            },
            {
                "group": "low-expectancy",
                "sample_ok": True,
                "expectancy_per_trade": "0.9",
                "max_drawdown": "0",
            },
            {
                "group": "high-drawdown",
                "sample_ok": True,
                "expectancy_per_trade": "1.1",
                "max_drawdown": "0.3",
            },
            {
                "group": "keep",
                "sample_ok": True,
                "expectancy_per_trade": "1.1",
                "max_drawdown": "0.1",
            },
        ]
    }
    calls: list[int] = []

    async def fake_get_scorecard(
        from_: datetime | None,
        to: datetime | None,
        group_by: str,
        minimum_trades: int,
    ) -> dict:
        calls.append(minimum_trades)
        return scorecard

    monkeypatch.setattr(api_module, "db_manager", None)
    with pytest.raises(HTTPException) as unavailable:
        await evaluate_scorecard(
            from_=None,
            to=None,
            minimum_trades=4,
        )
    assert unavailable.value.status_code == 503

    monkeypatch.setattr(
        api_module,
        "db_manager",
        SimpleNamespace(mongodb_adapter=object(), configuration=Configuration()),
    )
    monkeypatch.setattr(analysis_route, "get_scorecard", fake_get_scorecard)

    explicit = await evaluate_scorecard(minimum_trades=4)
    default = await evaluate_scorecard(minimum_trades=None)

    assert [group["status"] for group in explicit["groups"]] == [
        "watch",
        "disable",
        "disable",
        "keep",
    ]
    assert explicit["thresholds"]["scorecard_min_trades"] == 12
    assert default["status"] == "configured"
    assert calls == [4, 12]


@pytest.mark.asyncio
async def test_evaluate_is_unconfigured_and_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
