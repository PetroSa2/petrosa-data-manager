"""The netting | ground_truth switch and the exchange-truth serving contracts (petrosa-data-manager#842)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from data_manager.services import strategy_stats_source as src

NOW = datetime(2026, 11, 6, 22, 0, tzinfo=UTC)

SNAPSHOT = {
    "computed_at": NOW - timedelta(minutes=10),
    "rule": "A: quantity-share P&L, shared outcome counts for every participant",
    "window": {
        "start": "2026-10-07T22:00:00+00:00",
        "end": "2026-11-06T21:50:00+00:00",
    },
    "reconciliation": {"gap": 0.0},
    "strategies": {
        "iceberg_detector": {
            "performance": {
                "win_rate": 0.4,
                "win_rate_delta": None,
                "win_rate_delta_window": 12,
                "win_rate_delta_se": 0.2,
                "wins": 10,
                "losses": 15,
                "consecutive_losses": 1,
                "recent_pnl_trend": "negative",
                "realized_pnl": -330.0,
                "unrealized_pnl": None,
                "net_pnl": -347.07,
            },
            "rounds": {"fills": 900, "closed_rounds": 25, "wins": 10, "losses": 15},
        }
    },
}


def _db(parameters=None, raises=False):
    configuration = MagicMock()
    if raises:
        configuration.get_app_config = AsyncMock(side_effect=RuntimeError("mongo down"))
    else:
        config = (
            None if parameters is None else {"parameters": parameters, "version": 3}
        )
        configuration.get_app_config = AsyncMock(return_value=config)
    configuration.upsert_app_config = AsyncMock(return_value={"version": 4})
    db = MagicMock()
    db.configuration = configuration
    return db


@pytest.fixture(autouse=True)
def _fresh_cache():
    src.reset_state()
    yield
    src.reset_state()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parameters", "raises", "expected"),
    [
        (None, False, "netting"),  # no config yet
        ({"symbols": []}, False, "netting"),  # unset
        ({"strategy_stats_source": "bogus"}, False, "netting"),  # unknown value
        ({"strategy_stats_source": "ground_truth"}, False, "ground_truth"),
        (None, True, "netting"),  # unreadable
    ],
)
async def test_source_defaults_to_netting(parameters, raises, expected):
    assert await src.get_source(_db(parameters, raises)) == expected


@pytest.mark.asyncio
async def test_source_is_cached_for_a_minute():
    now = [0.0]
    db = _db({"strategy_stats_source": "ground_truth"})
    assert await src.get_source(db, clock=lambda: now[0]) == "ground_truth"
    now[0] = 59.0
    await src.get_source(db, clock=lambda: now[0])
    assert db.configuration.get_app_config.await_count == 1
    now[0] = 61.0
    await src.get_source(db, clock=lambda: now[0])
    assert db.configuration.get_app_config.await_count == 2


@pytest.mark.asyncio
async def test_set_source_merges_parameters_and_records_who_and_the_decision():
    db = _db({"symbols": ["BTCUSDT"], "max_positions": 10})
    await src.set_source(
        db,
        "ground_truth",
        changed_by="developer session",
        decision_ref="dm#842 comment 1",
    )
    kwargs = db.configuration.upsert_app_config.await_args.kwargs
    assert kwargs["parameters"] == {
        "symbols": ["BTCUSDT"],
        "max_positions": 10,
        "strategy_stats_source": "ground_truth",
    }
    assert kwargs["changed_by"] == "developer session"
    assert "decision: dm#842 comment 1" in kwargs["reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "by", "ref"),
    [("bogus", "x", "y"), ("netting", "", "y"), ("netting", "x", " ")],
)
async def test_set_source_refuses_bad_input(source, by, ref):
    db = _db({})
    with pytest.raises(ValueError) as err:
        await src.set_source(db, source, changed_by=by, decision_ref=ref)
    assert str(err.value)
    db.configuration.upsert_app_config.assert_not_awaited()


def test_performance_serves_the_snapshot_in_cios_contract():
    body = src.performance_from_snapshot(SNAPSHOT, "iceberg_detector", now=NOW)
    assert body["stats"]["wins"] == 10 and body["stats"]["losses"] == 15
    assert body["stats"]["consecutive_losses"] == 1
    meta = body["metadata"]
    assert meta["source"] == "data-manager-pnl-calculator"  # what CIO recognises
    assert meta["stats_source"] == "ground_truth"
    assert meta["history_status"] == "computed"
    assert meta["fills_replayed"] == 900
    assert "degraded" not in body


def test_a_strategy_without_episodes_is_unknown_not_zero():
    body = src.performance_from_snapshot(SNAPSHOT, "never_traded", now=NOW)
    assert body["stats"]["win_rate"] is None and body["stats"]["wins"] is None
    assert body["metadata"]["history_status"] == "zero_sample"


def test_a_stale_or_missing_snapshot_is_degraded_with_no_stats():
    old = {**SNAPSHOT, "computed_at": NOW - timedelta(minutes=31)}
    for snapshot in (old, None):
        body = src.performance_from_snapshot(snapshot, "iceberg_detector", now=NOW)
        assert body["degraded"] is True
        assert body["error"]["type"] == "GroundTruthStatsStale"
        assert body["stats"]["win_rate"] is None
        assert body["metadata"]["history_status"] == "unavailable"
        rounds = src.rounds_from_snapshot(snapshot, now=NOW)
        assert rounds["degraded"] is True and rounds["strategies"] == {}


def test_rounds_serves_every_strategy_with_the_reconciliation():
    body = src.rounds_from_snapshot(SNAPSHOT, now=NOW)
    assert body["strategies"]["iceberg_detector"]["closed_rounds"] == 25
    assert body["totals"]["closed_rounds"] == 25
    assert body["reconciliation"] == {"gap": 0.0}
    assert body["metadata"]["stats_source"] == "ground_truth"
    assert body["metadata"]["stale"] is False


@pytest.mark.asyncio
async def test_a_hung_config_read_falls_back_to_netting(monkeypatch):
    import asyncio

    monkeypatch.setattr(src, "SOURCE_READ_TIMEOUT_SECONDS", 0.05)

    async def hang():
        await asyncio.sleep(10)

    db = _db({})
    db.configuration.get_app_config = hang
    assert await src.get_source(db) == "netting"


@pytest.mark.asyncio
async def test_a_failed_read_after_the_flip_keeps_ground_truth(caplog):
    """A transient MongoDB failure after the flip never serves the known-wrong netting stats."""
    now = [0.0]
    db = _db({"strategy_stats_source": "ground_truth"})
    assert await src.get_source(db, clock=lambda: now[0]) == "ground_truth"

    db.configuration.get_app_config = AsyncMock(side_effect=RuntimeError("mongo down"))
    now[0] = 61.0  # the cache has expired: this is a real read, and it fails
    with caplog.at_level("WARNING", logger=src.__name__):
        assert await src.get_source(db, clock=lambda: now[0]) == "ground_truth"
    assert "config read failed" in caplog.text and "last read value" in caplog.text

    db.configuration.get_app_config = AsyncMock(
        return_value={"parameters": {"strategy_stats_source": "netting"}}
    )
    now[0] = 122.0  # a successful read still wins: the flip back takes effect
    assert await src.get_source(db, clock=lambda: now[0]) == "netting"


@pytest.mark.asyncio
async def test_a_failed_read_after_set_source_keeps_the_value_just_written():
    db = _db({"symbols": []})
    await src.set_source(db, "ground_truth", changed_by="x", decision_ref="y")
    db.configuration.get_app_config = AsyncMock(side_effect=RuntimeError("mongo down"))
    assert await src.get_source(db) == "ground_truth"


@pytest.mark.asyncio
async def test_a_process_that_never_read_the_config_falls_back_to_netting(caplog):
    with caplog.at_level("WARNING", logger=src.__name__):
        assert await src.get_source(_db(raises=True)) == "netting"
    assert "never read: default" in caplog.text
    no_repository = MagicMock(spec=[])
    src.reset_cache()
    assert await src.get_source(no_repository) == "netting"


def test_net_r_serves_each_strategys_keep_kill_input():
    snapshot = {
        **SNAPSHOT,
        "strategies": {
            "iceberg_detector": {
                **SNAPSHOT["strategies"]["iceberg_detector"],
                "net_r": {
                    "net_r": ["-1.5", "0.5"],
                    "closed_rounds": 25,
                    "rounds_without_risk": 23,
                },
            },
            "no_block": {"performance": {}},
        },
    }
    body = src.net_r_from_snapshot(snapshot, now=NOW)
    assert body["strategies"] == {
        "iceberg_detector": {
            "net_r": ["-1.5", "0.5"],
            "closed_rounds": 25,
            "rounds_without_risk": 23,
        }
    }
    assert body["metadata"]["stats_source"] == "ground_truth"


def test_stale_net_r_gives_no_verdict_input():
    old = {**SNAPSHOT, "computed_at": NOW - timedelta(hours=1)}
    body = src.net_r_from_snapshot(old, now=NOW)
    assert body["strategies"] == {} and body["degraded"] is True
