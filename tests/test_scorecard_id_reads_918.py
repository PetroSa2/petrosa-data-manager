"""petrosa-data-manager#918: stop and decision lookups read only the scored ids, never whole tables."""

from __future__ import annotations

from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from data_manager.api.routes import strategy_net_r


class FakeMySQL:
    """find_paginated over in-memory rows, honouring the IN filter and the column list."""

    def __init__(self, rows_by_table: dict[str, list[dict]]):
        self.rows_by_table = rows_by_table
        self.calls: list[dict] = []

    def query_range(self, *args, **kwargs):  # makes get_mysql_adapter accept it
        return []

    def find_paginated(self, table, *, filter_dict, limit, columns, include_total):
        self.calls.append(
            {
                "table": table,
                "filter": filter_dict,
                "columns": columns,
                "include_total": include_total,
            }
        )
        ((key, wanted),) = filter_dict.items()
        rows = [
            {c: row[c] for c in columns if c in row}
            for row in self.rows_by_table.get(table, [])
            if row.get(key) in wanted
        ]
        return rows[:limit], 0


def _manager(mysql: FakeMySQL):
    return SimpleNamespace(mysql_adapter=mysql, mongodb_adapter=None)


@pytest.mark.asyncio
async def test_stops_read_only_the_requested_positions_and_columns():
    mysql = FakeMySQL(
        {
            "positions": [
                {
                    "position_id": "p1",
                    "entry_price": 100,
                    "stop_loss": 98,
                    "metadata": "x" * 10_000,
                },
                {"position_id": "p2", "entry_price": 50, "stop_loss": 52},
                {"position_id": "other", "entry_price": 10, "stop_loss": 9},
            ]
        }
    )
    with patch.object(strategy_net_r.api_module, "db_manager", _manager(mysql)):
        stops = await strategy_net_r._load_stops(["p1", "p2", "missing"])

    assert stops == {"p1": D("0.02"), "p2": D("0.04")}
    (call,) = mysql.calls
    assert call["table"] == "positions"
    assert call["filter"] == {"position_id": ["p1", "p2", "missing"]}
    assert "metadata" not in call["columns"]
    assert call["include_total"] is False


@pytest.mark.asyncio
async def test_decisions_read_only_the_requested_ids_and_parse_a_text_payload():
    mysql = FakeMySQL(
        {
            "cio_decisions": [
                {
                    "decision_id": "d1",
                    "source": "cio_llm",
                    "payload": {"entry_price": 100, "stop_loss": 99},
                    "reasoning": "long text",
                },
                {"decision_id": "d2", "payload": '{"stop_loss_pct": "0.03"}'},
                {"decision_id": "d3", "payload": "not json"},
                {"decision_id": "d4", "payload": "[1, 2]"},
                {"decision_id": "unrelated", "payload": {"stop_loss_pct": "0.5"}},
            ]
        }
    )
    with patch.object(strategy_net_r.api_module, "db_manager", _manager(mysql)):
        decisions = await strategy_net_r._load_decisions(["d1", "d2", "d3", "d4"])

    assert decisions == {
        "d1": {"source": "cio_llm", "stop_fraction": D("0.01")},
        "d2": {"source": None, "stop_fraction": D("0.03")},
        "d3": {"source": None, "stop_fraction": None},
        "d4": {"source": None, "stop_fraction": None},
    }
    (call,) = mysql.calls
    assert call["filter"] == {"decision_id": ["d1", "d2", "d3", "d4"]}
    assert "reasoning" not in call["columns"]


@pytest.mark.asyncio
async def test_large_id_lists_are_read_in_bounded_chunks(monkeypatch):
    monkeypatch.setattr(strategy_net_r, "_ID_CHUNK", 2)
    ids = [f"p{i}" for i in range(5)]
    mysql = FakeMySQL(
        {
            "positions": [
                {"position_id": pid, "entry_price": 100, "stop_loss": 99} for pid in ids
            ]
        }
    )
    with patch.object(strategy_net_r.api_module, "db_manager", _manager(mysql)):
        stops = await strategy_net_r._load_stops(ids)

    assert set(stops) == set(ids)
    assert [c["filter"]["position_id"] for c in mysql.calls] == [
        ["p0", "p1"],
        ["p2", "p3"],
        ["p4"],
    ]


@pytest.mark.asyncio
async def test_no_ids_means_no_read():
    mysql = FakeMySQL({})
    with patch.object(strategy_net_r.api_module, "db_manager", _manager(mysql)):
        assert await strategy_net_r._load_stops([]) == {}
        assert await strategy_net_r._load_decisions([]) == {}
    assert mysql.calls == []
