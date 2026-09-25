from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from data_manager.maintenance.backfill_events_to_mysql import backfill_collection
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
