from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from data_manager.maintenance.index_usage_snapshot import counter_delta, run_snapshot


def test_counter_delta_detects_reset_and_dropped_indexes():
    previous = {
        "server_uptime_seconds": 10,
        "indexes": [{"table": "t", "index": "i", "rows_read": 4}],
    }
    current = {"server_uptime_seconds": 11, "indexes": []}

    result = counter_delta(previous, current)

    assert result == {"indexes": {}, "dropped": ["t.i"]}
    assert counter_delta(previous, {**current, "server_uptime_seconds": 9}) is None


def test_run_snapshot_embeds_rows_and_uses_named_ttl_index():
    result = MagicMock()
    result.mappings.return_value.fetchall.return_value = [
        {"TABLE_NAME": "t", "INDEX_NAME": "i", "ROWS_READ": 7},
    ]
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.side_effect = [
        result,
        MagicMock(
            mappings=MagicMock(
                return_value=MagicMock(
                    fetchall=MagicMock(
                        return_value=[
                            {
                                "TABLE_NAME": "t",
                                "ROWS_READ": 2,
                                "ROWS_CHANGED": 1,
                                "ROWS_CHANGED_X_INDEXES": 1,
                            }
                        ]
                    )
                )
            )
        ),
        MagicMock(
            mappings=MagicMock(
                return_value=MagicMock(
                    fetchall=MagicMock(return_value=[{"Value": "8"}])
                )
            )
        ),
    ]
    collection = MagicMock()
    collection.create_index = AsyncMock()
    collection.insert_one = AsyncMock()
    mongo = MagicMock(db={"mysql_index_usage_snapshots": collection})

    snapshot = __import__("asyncio").run(
        run_snapshot(
            engine,
            mongo,
            captured_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )

    assert len(snapshot["indexes"]) == 1
    collection.create_index.assert_awaited_once()
    assert collection.create_index.await_args.kwargs["name"] == "captured_at_ttl_90d"
    collection.insert_one.assert_awaited_once()
