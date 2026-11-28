"""petrosa-data-manager#925: the regime cache refresh reads a small, newest-first projection."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pymongo.errors import NetworkTimeout

import constants
from data_manager.consumer import execution_events_consumer as module
from data_manager.consumer.execution_events_consumer import ExecutionEventsConsumer
from data_manager.services.slippage_report import RegimeTimeline


class Cursor:
    def __init__(self, calls: list, docs: list, error: Exception | None):
        self.calls, self.docs, self.error = calls, docs, error

    def sort(self, keys):
        self.calls.append(("sort", keys))
        return self

    def limit(self, n):
        self.calls.append(("limit", n))
        return self

    async def to_list(self, length=None):
        self.calls.append(("to_list", length))
        if self.error is not None:
            raise self.error
        return self.docs


class Database:
    def __init__(self, failing: set[str]):
        self.failing = failing
        self.calls: dict[str, list] = {}

    def __getitem__(self, name):
        calls = self.calls.setdefault(name, [])
        failing = self.failing

        class Collection:
            def find(self, query, projection=None):
                calls.append(("find", query, projection))
                error = NetworkTimeout("timed out") if name in failing else None
                doc = {
                    "regime": "trending",
                    "metadata": {"computed_at": datetime(2026, 11, 27, tzinfo=UTC)},
                }
                return Cursor(calls, [doc], error)

        return Collection()


async def _refresh_once(consumer, monkeypatch):
    consumer.running = True

    async def stop(_seconds):
        consumer.running = False

    monkeypatch.setattr(asyncio, "sleep", stop)
    await consumer._refresh_regimes()


@pytest.mark.asyncio
async def test_refresh_reads_a_projected_newest_first_bounded_window(monkeypatch):
    db = Database(failing=set())
    consumer = ExecutionEventsConsumer(
        db_manager=SimpleNamespace(mongodb_adapter=SimpleNamespace(db=db))
    )
    await _refresh_once(consumer, monkeypatch)

    symbol = constants.SUPPORTED_PAIRS[0].strip()
    calls = db.calls[f"analytics_{symbol}_regime"]
    (_, query, projection) = calls[0]
    assert query == {}
    assert projection == {
        "_id": 0,
        "regime": 1,
        "computed_at": 1,
        "metadata.computed_at": 1,
        "timestamp": 1,
    }
    assert ("sort", [("_id", -1)]) in calls
    assert ("limit", module._REGIME_REFRESH_LIMIT) in calls
    assert ("to_list", module._REGIME_REFRESH_LIMIT) in calls
    # the projected document still dates the regime for a fill stamp
    timeline = RegimeTimeline(consumer._regime_cache[symbol][1])
    assert timeline.at(datetime(2026, 11, 28, tzinfo=UTC)) == "trending"


@pytest.mark.asyncio
async def test_a_timeout_keeps_that_symbols_last_timeline_and_refreshes_the_rest(
    monkeypatch, caplog
):
    pairs = [p.strip() for p in constants.SUPPORTED_PAIRS]
    failing, healthy = pairs[0], pairs[1]
    db = Database(failing={f"analytics_{failing}_regime"})
    consumer = ExecutionEventsConsumer(
        db_manager=SimpleNamespace(mongodb_adapter=SimpleNamespace(db=db))
    )
    previous = (datetime(2026, 11, 26, tzinfo=UTC), [{"regime": "old"}])
    consumer._regime_cache[failing] = previous

    with caplog.at_level(logging.WARNING):
        await _refresh_once(consumer, monkeypatch)

    assert consumer._regime_cache[failing] is previous
    assert consumer._regime_cache[healthy][1][0]["regime"] == "trending"
    (record,) = [
        r for r in caplog.records if "Could not refresh regime cache" in r.getMessage()
    ]
    assert failing in record.getMessage() and "NetworkTimeout" in record.getMessage()
    assert record.exc_info is None  # one line, not a traceback
