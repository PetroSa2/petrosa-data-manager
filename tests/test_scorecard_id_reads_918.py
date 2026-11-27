"""petrosa-data-manager#918: stop and decision lookups read only the scored ids, never whole tables."""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import sqlalchemy as sa

from data_manager.api.routes import strategy_net_r
from data_manager.db.mysql_adapter import MySQLAdapter


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


def _decision(decision_id: str, pct: str = "0.02") -> dict:
    return {"decision_id": decision_id, "payload": {"stop_loss_pct": pct}}


@pytest.mark.asyncio
async def test_decision_rows_are_cached_and_only_unseen_ids_are_read():
    mysql = FakeMySQL({"cio_decisions": [_decision("d1"), _decision("d2")]})
    with patch.object(strategy_net_r.api_module, "db_manager", _manager(mysql)):
        first = await strategy_net_r._load_decisions(["d1"])
        second = await strategy_net_r._load_decisions(["d1", "d2", "absent"])

    assert set(first) == {"d1"}
    assert set(second) == {"d1", "d2"}
    assert [c["filter"]["decision_id"] for c in mysql.calls] == [
        ["d1"],
        ["d2", "absent"],  # d1 came from the cache; an absent id is asked again
    ]


@pytest.mark.asyncio
async def test_a_timed_out_decision_read_still_fills_the_cache():
    release = threading.Event()
    mysql = FakeMySQL({"cio_decisions": [_decision("d1")]})
    inner = mysql.find_paginated

    def slow(*args, **kwargs):
        release.wait(2)
        return inner(*args, **kwargs)

    mysql.find_paginated = slow
    with patch.object(strategy_net_r.api_module, "db_manager", _manager(mysql)):
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(strategy_net_r._load_decisions(["d1"]), 0.05)
        with pytest.raises(
            asyncio.TimeoutError
        ):  # the next request joins the same read
            await asyncio.wait_for(strategy_net_r._load_decisions(["d1"]), 0.05)
        release.set()
        decisions = await asyncio.wait_for(strategy_net_r._load_decisions(["d1"]), 2)

    assert decisions["d1"]["stop_fraction"] == D("0.02")
    assert len(mysql.calls) == 1


@pytest.fixture
def sqlite_mysql():
    """The real adapter on SQLite, with a reflected positions table that has no avg_price."""
    adapter = MySQLAdapter("sqlite:///:memory:", role="serving")
    adapter.engine_options = {}
    adapter.engine = sa.create_engine(
        "sqlite://",
        poolclass=sa.pool.StaticPool,
        connect_args={"check_same_thread": False},
    )
    adapter._connected = True
    adapter._create_tables()
    when = datetime(2026, 11, 26)
    with adapter.engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE positions (id INTEGER PRIMARY KEY, position_id VARCHAR(64) UNIQUE,"
            " entry_price NUMERIC(20,8), stop_loss NUMERIC(20,8), metadata TEXT)"
        )
        conn.exec_driver_sql(
            "INSERT INTO positions (position_id, entry_price, stop_loss, metadata)"
            " VALUES ('p1', 100, 98, 'big'), ('other', 1, 1, 'big')"
        )
        conn.execute(
            adapter.tables["cio_decisions"].insert(),
            [
                {
                    "decision_id": "d1",
                    "strategy_id": "s",
                    "timestamp": when,
                    "source": "cio_llm",
                    "payload": {"entry_price": 100, "stop_loss": 99},
                    "reasoning": {"text": "long"},
                    "received_at": when,
                }
            ],
        )
    statements: list[str] = []
    sa.event.listen(
        adapter.engine,
        "before_cursor_execute",
        lambda _c, _cur, statement, *_rest: statements.append(statement.lower()),
    )
    return adapter, statements


@pytest.mark.asyncio
async def test_the_real_adapter_reads_by_id_without_a_count_or_wide_columns(
    sqlite_mysql,
):
    adapter, statements = sqlite_mysql
    manager = SimpleNamespace(mysql_adapter=adapter, mongodb_adapter=None)
    with patch.object(strategy_net_r.api_module, "db_manager", manager):
        stops = await strategy_net_r._load_stops(["p1", "missing"])
        decisions = await strategy_net_r._load_decisions(["d1", "dx"])

    assert stops == {"p1": D("0.02")}
    assert decisions == {"d1": {"source": "cio_llm", "stop_fraction": D("0.01")}}
    selects = [
        s
        for s in statements
        if s.lstrip().startswith("select")
        and ("from positions" in s or "from cio_decisions" in s)
    ]
    assert len(selects) == 2
    assert all(" in (" in s for s in selects)
    assert not any("count(" in s for s in selects)
    assert not any("metadata" in s or "reasoning" in s for s in selects)


@pytest.mark.asyncio
async def test_requests_waiting_on_one_read_do_not_each_read_the_same_ids():
    """Review of #920: waiters re-check after the in-flight read instead of all reading again."""
    release = threading.Event()
    mysql = FakeMySQL({"cio_decisions": [_decision("d1"), _decision("d2")]})
    inner = mysql.find_paginated

    def slow(*args, **kwargs):
        release.wait(2)
        return inner(*args, **kwargs)

    mysql.find_paginated = slow
    with patch.object(strategy_net_r.api_module, "db_manager", _manager(mysql)):
        first = asyncio.ensure_future(strategy_net_r._load_decisions(["d1"]))
        await asyncio.sleep(0.01)
        waiters = [
            asyncio.ensure_future(strategy_net_r._load_decisions(ids))
            for ids in (["d2"], ["d2"], ["d2", "d3"])
        ]
        await asyncio.sleep(0.01)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(first, *waiters), 2)

    assert "d1" in results[0] and all("d2" in r for r in results[1:])
    reads = [c["filter"]["decision_id"] for c in mysql.calls]
    assert sum("d2" in ids for ids in reads) == 1  # d2 read once, not once per waiter
