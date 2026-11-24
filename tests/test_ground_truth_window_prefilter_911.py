"""petrosa-data-manager#911: the window is filtered in the query, with exactly the old result."""

from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from mongomock_motor import AsyncMongoMockClient

from data_manager.services import exchange_ground_truth_inputs as module

START = datetime(2026, 10, 25, 16, 0, tzinfo=UTC)
END = datetime(2026, 11, 24, 16, 0, tzinfo=UTC)


def _rows():
    inside = START + timedelta(days=3)
    before = START - timedelta(days=10)
    after = END + timedelta(days=10)
    edge_low = START
    edge_high = END
    return [
        # datetimes, aware and naive
        {
            "strategy_position_id": "dt-entry-in",
            "entry_time": inside,
            "exit_time": None,
        },
        {
            "strategy_position_id": "dt-naive-in",
            "entry_time": inside.replace(tzinfo=None),
        },
        {
            "strategy_position_id": "dt-exit-in-entry-before",
            "entry_time": before,
            "exit_time": inside,
        },
        {
            "strategy_position_id": "dt-out",
            "entry_time": before,
            "exit_time": before + timedelta(hours=1),
        },
        {"strategy_position_id": "dt-after", "entry_time": after},
        {"strategy_position_id": "dt-edge-low", "entry_time": edge_low},
        {"strategy_position_id": "dt-edge-high", "exit_time": edge_high},
        {
            "strategy_position_id": "dt-just-before",
            "entry_time": START - timedelta(seconds=1),
        },
        # ISO strings in the forms seen in the collection
        {
            "strategy_position_id": "s-z",
            "entry_time": inside.isoformat().replace("+00:00", "Z"),
        },
        {"strategy_position_id": "s-offset", "entry_time": inside.isoformat()},
        {
            "strategy_position_id": "s-naive-space",
            "entry_time": inside.replace(tzinfo=None).isoformat(sep=" "),
        },
        {
            "strategy_position_id": "s-exit-in",
            "entry_time": before.isoformat(),
            "exit_time": inside.isoformat(),
        },
        # +05:00 on the next local day but inside the window in UTC
        {
            "strategy_position_id": "s-plus5-edge",
            "exit_time": (END - timedelta(hours=1))
            .astimezone(timezone(timedelta(hours=5)))
            .isoformat(),
        },
        # -05:00 whose local date is before the window start but UTC is inside
        {
            "strategy_position_id": "s-minus5-edge",
            "entry_time": (START + timedelta(hours=1))
            .astimezone(timezone(timedelta(hours=-5)))
            .isoformat(),
        },
        {
            "strategy_position_id": "s-out",
            "entry_time": before.isoformat(),
            "exit_time": before.isoformat(),
        },
        {"strategy_position_id": "s-garbage", "entry_time": "not-a-time"},
        # forms fromisoformat accepts without the dashed date prefix
        {
            "strategy_position_id": "s-compact",
            "entry_time": inside.strftime("%Y%m%dT%H%M%SZ"),
        },
        {"strategy_position_id": "s-week", "exit_time": inside.strftime("%G-W%V-%u")},
        {"strategy_position_id": "missing-times"},
    ]


def _old_semantics(rows):
    return sorted(
        r["strategy_position_id"] for r in rows if module._in_window(r, START, END)
    )


@pytest.mark.asyncio
async def test_query_prefilter_returns_exactly_the_python_window_rows():
    db = AsyncMongoMockClient(tz_aware=True).test
    rows = _rows()
    await db.strategy_positions.insert_many([dict(r) for r in rows])

    got = await module.read_position_rows(SimpleNamespace(db=db), START, END)

    expected = _old_semantics(rows)
    assert sorted(r["strategy_position_id"] for r in got) == expected
    assert "dt-exit-in-entry-before" in expected
    assert "s-minus5-edge" in expected
    assert "dt-just-before" not in expected
    assert {"s-compact", "s-week"} <= set(expected)


@pytest.mark.asyncio
async def test_query_prefilter_leaves_out_rows_far_outside_the_window():
    db = AsyncMongoMockClient(tz_aware=True).test
    await db.strategy_positions.insert_many([dict(r) for r in _rows()])

    raw = await db.strategy_positions.find(
        module._window_prefilter(START, END)
    ).to_list(length=None)

    ids = {r["strategy_position_id"] for r in raw}
    assert not ids & {"dt-out", "dt-after", "s-out", "missing-times", "s-garbage"}
