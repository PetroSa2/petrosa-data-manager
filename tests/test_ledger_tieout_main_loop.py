"""Ledger tie-out reads Motor on the event loop, never from a worker thread (#837)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from data_manager.db.repositories.ledger_repository import LedgerRepository


class _LoopBoundCursor:
    """Fails like Motor does when awaited on a loop other than the one it was created for."""

    def __init__(self, loop, rows):
        self._loop = loop
        self._rows = rows

    def limit(self, _n):
        return self

    async def to_list(self, length):
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("got Future attached to a different loop")
        return self._rows[:length]


def _mongo(loop, rows_by_collection):
    collections = {
        name: SimpleNamespace(
            find=lambda *_a, _rows=rows: _LoopBoundCursor(loop, _rows)
        )
        for name, rows in rows_by_collection.items()
    }
    return SimpleNamespace(db=collections)


def _repo(mongodb):
    repo = LedgerRepository.__new__(LedgerRepository)
    repo.mysql = None
    repo.mongodb = mongodb
    return repo


@pytest.mark.asyncio
async def test_positions_tieout_on_loop_reads_motor_on_the_running_loop():
    loop = asyncio.get_running_loop()
    repo = _repo(
        _mongo(
            loop,
            {
                "positions": [{"entry_order_id": "o-1"}],
                "strategy_positions": [{"entry_order_id": 2}],
            },
        )
    )
    seen: dict = {}

    def fake_positions_tieout(*, external_close_ids=None):
        seen["ids"] = external_close_ids
        seen["in_loop_thread"] = False
        try:
            asyncio.get_running_loop()
            seen["in_loop_thread"] = True
        except RuntimeError:
            pass
        return {"phantom_rows": []}

    repo.positions_tieout = fake_positions_tieout

    result = await repo.positions_tieout_on_loop()

    assert result == {"phantom_rows": []}
    assert seen["ids"] == {"o-1", "2"}
    assert seen["in_loop_thread"] is False  # the SQL part still runs in a worker thread


@pytest.mark.asyncio
async def test_sync_tieout_refuses_to_read_motor_itself():
    repo = _repo(_mongo(asyncio.get_running_loop(), {}))
    with pytest.raises(RuntimeError, match="positions_tieout_on_loop"):
        await asyncio.to_thread(repo.positions_tieout)


@pytest.mark.asyncio
async def test_historic_only_without_mongo_reads_no_live_ids():
    repo = _repo(None)
    repo.historic_only = True
    assert await repo.live_closed_entry_order_ids_for_tieout() == set()
    repo.historic_only = False
    with pytest.raises(RuntimeError, match="MongoDB is not available"):
        await repo.live_closed_entry_order_ids_for_tieout()
