"""petrosa-data-manager#881: the copy loop releases quarantines in bounded batches, pauses on re-quarantine, and
reports the quarantined count on a gauge."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from mongomock_motor import AsyncMongoMockClient

from data_manager.api.routes import trading_state as module
from data_manager.db.repositories.trading_state_repository import (
    TradingStateRepository,
)


class _MySQL:
    def __init__(self):
        self.rows: dict[str, dict] = {}

    def get_column_names(self, table):
        return {"position_id", "status", "exit_price"}

    def persist_trading_state(self, table, data, version):
        self.rows[data["position_id"]] = data


@pytest.fixture
def loop_env(monkeypatch):
    monkeypatch.setenv("PETROSA_TRADING_MYSQL_COPY_ENABLED", "true")
    mongo = SimpleNamespace(
        db=AsyncMongoMockClient().loop, _prepare_for_bson=lambda value: value
    )
    mysql = _MySQL()
    monkeypatch.setattr(
        module.api_module,
        "db_manager",
        SimpleNamespace(mysql_adapter=mysql, mongodb_adapter=mongo),
    )
    gauge = MagicMock()
    monkeypatch.setattr(module, "TRADING_STATE_QUARANTINED_DOCUMENTS", gauge)
    monkeypatch.setattr(module, "TRADING_STATE_RECOVERY_EVENTS", MagicMock())
    monkeypatch.setattr(
        module,
        "_release_state",
        {
            "last_release_at": float("-inf"),
            "quarantined_at_release": None,
            "paused_until": 0.0,
        },
    )
    monkeypatch.setattr(module, "_copy_state", {**module._copy_state, "quarantined": 0})
    monkeypatch.setattr(module, "_RELEASE_BATCH", 2)
    repo = TradingStateRepository(mysql, mongo)
    return repo, mysql, gauge


async def _quarantined_positions(repo, n: int, status: str = "closed") -> None:
    for i in range(n):
        await repo.create_position({"position_id": f"q{i}", "status": status})
    await repo._collection("positions").update_many(
        {}, {"$set": {"_mysql_copy_quarantined": True}}
    )


def _allow_next_release(monkeypatch):
    module._release_state["last_release_at"] = float("-inf")


@pytest.mark.asyncio
async def test_release_is_one_bounded_batch_per_interval_and_sets_the_gauge(loop_env):
    repo, _, gauge = loop_env
    await _quarantined_positions(repo, 5)

    await module._release_quarantines(repo)
    assert await repo.quarantined_count("positions") == 3
    gauge.set.assert_any_call(3, {"table": "positions"})

    await module._release_quarantines(repo)  # same interval: nothing more
    assert await repo.quarantined_count("positions") == 3


@pytest.mark.asyncio
async def test_released_quarantines_are_copied_to_mysql(loop_env, monkeypatch):
    repo, mysql, gauge = loop_env
    await _quarantined_positions(repo, 3)

    for _ in range(3):
        _allow_next_release(monkeypatch)
        await module._replay_mysql_copies()

    assert set(mysql.rows) == {"q0", "q1", "q2"}
    assert all(row["status"] == "closed" for row in mysql.rows.values())
    assert await repo.quarantined_count("positions") == 0
    gauge.set.assert_called_with(0, {"table": "daily_pnl"})
    assert any(
        call.args == (0, {"table": "positions"}) for call in gauge.set.call_args_list
    )


@pytest.mark.asyncio
async def test_a_re_quarantine_pauses_further_releases(loop_env, monkeypatch, caplog):
    repo, _, _ = loop_env
    await _quarantined_positions(repo, 5)

    await module._release_quarantines(repo)  # releases 2
    module._copy_state["quarantined"] += 1  # one of them failed again
    _allow_next_release(monkeypatch)
    await module._release_quarantines(repo)

    assert await repo.quarantined_count("positions") == 3  # nothing more released
    assert module._release_state["paused_until"] > 0
    assert "trading_state_mysql_quarantine_release_paused" in caplog.text


@pytest.mark.asyncio
async def test_no_quarantine_means_no_release_and_a_zero_gauge(loop_env):
    repo, _, gauge = loop_env
    await repo.create_position({"position_id": "fine", "status": "open"})

    await module._release_quarantines(repo)

    gauge.set.assert_any_call(0, {"table": "positions"})
    assert module._release_state["quarantined_at_release"] is None
