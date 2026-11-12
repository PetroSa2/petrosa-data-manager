import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

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
    assert group["uncertainty"]["expectancy_per_trade"]["n"] == 1
    assert group["uncertainty"]["expectancy_per_trade"]["low"] == "8.65"
    assert group["uncertainty"]["expectancy_per_trade"]["high"] == "8.65"
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
        "filters": {"event_type": {"$in": ["filled", "partial_fill"]}},
        "start": start,
        "end": end,
        "limit": 10000,
        "sort_order": 1,
    }
    assert mongodb.calls[-1][1] == {"limit": 10000, "sort_order": 1}


@pytest.mark.asyncio
async def test_scorecard_read_timeout_is_explicit_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Mongo:
        async def find_filtered(self, collection: str, **kwargs: object) -> list[dict]:
            if collection == "execution_events":
                await asyncio.sleep(0.05)
            return []

    import data_manager.services.scorecard_service as scorecard_module

    monkeypatch.setattr(
        scorecard_module.constants, "ANALYTICS_DEPENDENCY_TIMEOUT_SECONDS", 0.001
    )
    result = await ScorecardService(Mongo()).calculate(minimum_trades=1)

    assert result["status"] == "incomplete"
    assert result["source_errors"] == {"execution_events": "read timed out"}


@pytest.mark.asyncio
async def test_scorecard_reads_are_concurrent_within_the_shared_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    class Mongo:
        async def find_filtered(self, collection: str, **kwargs: object) -> list[dict]:
            await asyncio.sleep(0.01)
            return []

    import data_manager.services.scorecard_service as scorecard_module

    monkeypatch.setattr(
        scorecard_module.constants, "ANALYTICS_DEPENDENCY_TIMEOUT_SECONDS", 0.05
    )
    started = time.monotonic()
    result = await ScorecardService(Mongo()).calculate(minimum_trades=1)

    assert result["groups"] == []
    assert time.monotonic() - started < 0.05


@pytest.mark.asyncio
async def test_scorecard_all_source_failures_are_unavailable() -> None:
    class Mongo:
        async def find_filtered(self, collection: str, **kwargs: object) -> list[dict]:
            raise RuntimeError(f"{collection} unavailable")

    result = await ScorecardService(Mongo()).calculate(minimum_trades=1)

    assert result["status"] == "unavailable"
    assert set(result["source_errors"]) == {
        "execution_events",
        "pnl_events",
        "cio_decisions",
        "funding_events",
        "positions",
    }


@pytest.mark.asyncio
async def test_scorecard_production_sized_read_stays_within_budget() -> None:
    import time

    rows = [_close("1", position_id=f"p{index}") for index in range(10_000)]

    class Mongo:
        async def find_filtered(self, collection: str, **kwargs: object) -> list[dict]:
            return rows if collection == "execution_events" else []

    started = time.monotonic()
    result = await ScorecardService(Mongo())._read_collections(None, None)

    assert len(result["execution_events"]) == 10_000
    assert time.monotonic() - started < 2.0
