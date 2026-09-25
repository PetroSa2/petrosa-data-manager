from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from data_manager.maintenance.backfill_events_to_mysql import (
    _parse_args,
    backfill_collection,
    run_backfill,
)
from data_manager.models.pnl_event import PnlEvent


class FakeCursor:
    def __init__(self, documents):
        self.documents = list(documents)

    async def to_list(self, length):
        batch = self.documents[:length]
        self.documents = self.documents[length:]
        return batch


def _doc(index):
    timestamp = datetime(2026, 9, 25, 12, 0, index, tzinfo=UTC)
    return {
        "_id": f"d{index}",
        "decision_id": f"decision-{index}",
        "strategy_id": "strategy-1",
        "timestamp": timestamp,
        "pnl_kind": "closed",
        "realized_pnl_usd": float(index),
        "order_id": f"order-{index}",
        "payload": {"source": "fixture"},
        "received_at": timestamp,
    }


def test_backfill_accepts_explicit_dry_run():
    assert _parse_args(["--dry-run"]).apply is False


@pytest.mark.asyncio
async def test_backfill_skips_invalid_documents_and_handles_both_collections():
    collection = MagicMock()
    collection.find.return_value = FakeCursor([{"not": "an event"}])
    mongo = SimpleNamespace(db={"execution_events": collection, "pnl_events": collection})

    counts = await run_backfill(mongo, MagicMock(), dry_run=True)

    assert counts == {"execution_events": 0, "pnl_events": 0}


@pytest.mark.asyncio
async def test_amain_connects_runs_and_disconnects():
    module = __import__(
        "data_manager.maintenance.backfill_events_to_mysql",
        fromlist=["_amain"],
    )
    mongo = MagicMock()
    mysql = MagicMock()
    args = SimpleNamespace(apply=False, batch_size=10)
    with (
        patch.object(module, "MongoDBAdapter", return_value=mongo),
        patch.object(module, "MySQLAdapter", return_value=mysql),
        patch.object(
            module,
            "run_backfill",
            return_value={"execution_events": 1, "pnl_events": 2},
        ),
    ):
        assert await module._amain(args) == 0
    mongo.connect.assert_called_once()
    mysql.connect.assert_called_once()
    mongo.disconnect.assert_called_once()
    mysql.disconnect.assert_called_once()


@pytest.mark.asyncio
async def test_backfill_dry_run_reports_count_without_writes():
    collection = MagicMock()
    collection.find.return_value = FakeCursor([_doc(1), _doc(2), _doc(3)])
    mysql = MagicMock()

    count = await backfill_collection(
        collection,
        mysql,
        "pnl_events",
        PnlEvent,
        batch_size=2,
        dry_run=True,
    )

    assert count == 3
    mysql.write.assert_not_called()


@pytest.mark.asyncio
async def test_backfill_apply_writes_each_batch():
    collection = MagicMock()
    collection.find.return_value = FakeCursor([_doc(1), _doc(2), _doc(3)])
    mysql = MagicMock()

    count = await backfill_collection(
        collection,
        mysql,
        "pnl_events",
        PnlEvent,
        batch_size=2,
        dry_run=False,
    )

    assert count == 3
    assert mysql.write.call_count == 2
    written = [record for call in mysql.write.call_args_list for record in call.args[0]]
    assert len(written) == 3
    assert all(isinstance(record, PnlEvent) for record in written)
