"""Acceptance-criteria coverage for petrosa-data-manager#346.

Complements ``tests/test_346_column_projection.py`` with the ACs that need
the *real* adapter methods (statements captured from ``conn.execute`` on an
in-memory SQLite engine, compiled with the MySQL dialect) rather than the
``_select_table_columns`` helper compared against itself:

* AC1  -- ``columns=None`` emits exactly the pre-change ``select(table)`` SQL
          for ``query_range``, ``query_latest`` and ``find_paginated``.
* AC2  -- the candle-path statement names only ``MYSQL_CANDLE_COLUMNS``.
* AC4  -- ``columns=[]`` falls back to the whole table *and* warns.
* AC7  -- ``map_mysql_row`` output is identical for projected vs full rows.
* AC8  -- ``get_candles`` response shape is unchanged on projected rows.
* AC9  -- ``row_to_candle`` on the 11-column warm-up projection, including
          the ``open_time`` timestamp fallback.
* AC10 -- the generic API pushes ``field_list`` down, with response parity
          against the old post-fetch filtering.
* AC11 -- ``total`` counts the full filtered set when a projection is set.
* AC12 -- every MySQL read call on the klines read path passes ``columns=``.
"""

import ast
import inspect
import re
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
import sqlalchemy as sa
from sqlalchemy import and_, select
from sqlalchemy.dialects import mysql
from sqlalchemy.pool import StaticPool

import data_manager.api.app as api_module
import data_manager.db.repositories.candle_repository as candle_repository_module
import data_manager.maintenance.candle_warmup_backfill as warmup_module
from data_manager.api.routes.data import get_candles
from data_manager.api.routes.generic import _execute_query_internal
from data_manager.db.mongodb_adapter import MongoDBAdapter
from data_manager.db.mysql_adapter import MySQLAdapter
from data_manager.db.repositories.candle_repository import (
    _MYSQL_COLUMN_MAP,
    MYSQL_CANDLE_COLUMNS,
    CandleRepository,
    map_mysql_row,
)
from data_manager.maintenance.candle_warmup_backfill import (
    BackfillConfig,
    backfill_pair,
    row_to_candle,
)

TABLE = "klines_h1"  # mysql_table_name("1h")
WARMUP_COLUMNS = MYSQL_CANDLE_COLUMNS + (
    "open_time",
    "quote_asset_volume",
    "number_of_trades",
)
# Issue #346 AC2: columns that must never appear on the candle-path statement.
CANDLE_PATH_WASTE = {
    "id",
    "open_time",
    "close_time",
    "extracted_at",
    "extractor_version",
    "source",
    "price_change",
    "price_change_percent",
}
# Issue #346 "The consumer that narrows this ticket": what the 11-column
# warm-up projection still drops.
WARMUP_PATH_WASTE = {
    "id",
    "close_time",
    "extracted_at",
    "extractor_version",
    "source",
    "price_change",
    "price_change_percent",
    "taker_buy_base_asset_volume",
    "taker_buy_quote_asset_volume",
}
ALL_KLINES_COLUMNS = {
    "id",
    "symbol",
    "timestamp",
    "open_time",
    "close_time",
    "interval",
    "open_price",
    "high_price",
    "low_price",
    "close_price",
    "volume",
    "quote_asset_volume",
    "number_of_trades",
    "taker_buy_base_asset_volume",
    "taker_buy_quote_asset_volume",
    "price_change",
    "price_change_percent",
    "extracted_at",
    "extractor_version",
    "source",
}


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class _RecordingConnection:
    def __init__(self, conn, log):
        self._conn = conn
        self._log = log

    def execute(self, statement, *args, **kwargs):
        self._log.append(statement)
        return self._conn.execute(statement, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class _RecordingEngine:
    """Engine proxy that records every statement passed to ``conn.execute``."""

    def __init__(self, engine):
        self._engine = engine
        self.statements: list = []

    @contextmanager
    def connect(self):
        with self._engine.connect() as conn:
            yield _RecordingConnection(conn, self.statements)

    def __getattr__(self, name):
        return getattr(self._engine, name)


@pytest.fixture
def recording_adapter():
    """Real ``MySQLAdapter`` on a thread-safe in-memory SQLite engine.

    ``StaticPool`` keeps a single connection so ``asyncio.to_thread`` reads
    (``CandleRepository``) see the same in-memory database.
    """
    adapter = MySQLAdapter("sqlite:///:memory:")
    adapter.engine_options = {}
    adapter.engine = sa.create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    adapter._connected = True
    recorder = _RecordingEngine(adapter.engine)
    adapter._ensure_connected = lambda: recorder
    adapter.recorder = recorder
    return adapter


def _row(hour: int, symbol: str = "BTCUSDT") -> dict:
    ts = datetime(2026, 1, 1, hour)
    return {
        "id": f"{symbol}-{hour}",
        "symbol": symbol,
        "timestamp": ts,
        "open_time": ts,
        "close_time": ts + timedelta(minutes=59),
        "interval": "1h",
        "open_price": Decimal("100.5") + hour,
        "high_price": Decimal("110.25") + hour,
        "low_price": Decimal("90.125") + hour,
        "close_price": Decimal("105.75") + hour,
        "volume": Decimal("1000") + hour,
        "quote_asset_volume": Decimal("2000") + hour,
        "number_of_trades": 300 + hour,
        "taker_buy_base_asset_volume": Decimal("5"),
        "taker_buy_quote_asset_volume": Decimal("6"),
        "price_change": Decimal("5.25"),
        "price_change_percent": Decimal("5.2239"),
        "extracted_at": ts,
        "extractor_version": "test",
        "source": "test",
    }


def _seed(adapter, rows) -> None:
    table = adapter._get_table(TABLE)
    with adapter.engine.begin() as conn:
        conn.execute(table.insert(), rows)


def _sql(statement) -> str:
    return str(statement.compile(dialect=mysql.dialect()))


def _params(statement) -> dict:
    return statement.compile(dialect=mysql.dialect()).params


def _selected(statement) -> list[str]:
    return [column.name for column in statement.selected_columns]


def _select_clause_columns(statement, table_name: str = TABLE) -> set[str]:
    select_clause = re.split(r"\sFROM\s", _sql(statement), maxsplit=1)[0]
    return set(re.findall(rf"{table_name}\.`?(\w+)`?", select_clause))


def _read_statements(adapter) -> list:
    """Statements that select from a table (excludes ``COUNT(*)``)."""
    return [
        s
        for s in adapter.recorder.statements
        if isinstance(s, sa.Select) and "count(" not in _sql(s).lower()
    ]


# ---------------------------------------------------------------------------
# AC1 -- columns=None is character-identical to the pre-change select(table)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["query_range", "query_latest", "find_paginated"])
def test_ac1_columns_is_keyword_only_and_defaults_to_none(method):
    parameter = inspect.signature(getattr(MySQLAdapter, method)).parameters["columns"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is None


def test_ac1_query_range_columns_none_matches_pre_change_sql(recording_adapter):
    _seed(recording_adapter, [_row(1)])
    table = recording_adapter._get_table(TABLE)
    time_col = table.c.timestamp
    start, end = datetime(2026, 1, 1), datetime(2026, 1, 2)

    recording_adapter.query_range(
        TABLE, start, end, "BTCUSDT", limit=5, offset=2, descending=True
    )
    recording_adapter.query_range(TABLE, start, end)
    captured = _read_statements(recording_adapter)
    assert len(captured) == 2

    # Pre-change body of query_range, reproduced verbatim from origin/main.
    pre_change_full = (
        select(table)
        .where(and_(time_col >= start, time_col < end))
        .where(table.c.symbol == "BTCUSDT")
        .order_by(time_col.desc())
        .limit(5)
        .offset(2)
    )
    pre_change_bare = select(table).where(and_(time_col >= start, time_col < end))
    pre_change_bare = pre_change_bare.order_by(time_col)

    assert _sql(captured[0]) == _sql(pre_change_full)
    assert _params(captured[0]) == _params(pre_change_full)
    assert _sql(captured[1]) == _sql(pre_change_bare)
    assert set(_selected(captured[0])) == ALL_KLINES_COLUMNS


def test_ac1_query_latest_columns_none_matches_pre_change_sql(recording_adapter):
    _seed(recording_adapter, [_row(1)])
    table = recording_adapter._get_table(TABLE)
    time_col = table.c.timestamp

    recording_adapter.query_latest(TABLE, "BTCUSDT", 3)
    recording_adapter.query_latest(TABLE)
    captured = _read_statements(recording_adapter)
    assert len(captured) == 2

    pre_change_symbol = (
        select(table)
        .where(table.c.symbol == "BTCUSDT")
        .order_by(time_col.desc())
        .limit(3)
    )
    pre_change_bare = select(table).order_by(time_col.desc()).limit(1)

    assert _sql(captured[0]) == _sql(pre_change_symbol)
    assert _params(captured[0]) == _params(pre_change_symbol)
    assert _sql(captured[1]) == _sql(pre_change_bare)
    assert set(_selected(captured[0])) == ALL_KLINES_COLUMNS


def test_ac1_find_paginated_columns_none_matches_pre_change_sql(recording_adapter):
    _seed(recording_adapter, [_row(1)])
    table = recording_adapter._get_table(TABLE)

    recording_adapter.find_paginated(
        TABLE,
        filter_dict={"symbol": "BTCUSDT"},
        sort_list=[("timestamp", -1), ("bogus", 1)],
        limit=10,
        offset=4,
    )
    recording_adapter.find_paginated(TABLE)
    captured = _read_statements(recording_adapter)
    assert len(captured) == 2

    pre_change_filtered = (
        select(table)
        .where(and_(table.c.symbol == "BTCUSDT"))
        .order_by(table.c.timestamp.desc())
        .limit(10)
        .offset(4)
    )
    pre_change_bare = select(table).limit(100).offset(0)

    assert _sql(captured[0]) == _sql(pre_change_filtered)
    assert _params(captured[0]) == _params(pre_change_filtered)
    assert _sql(captured[1]) == _sql(pre_change_bare)
    assert set(_selected(captured[0])) == ALL_KLINES_COLUMNS


# ---------------------------------------------------------------------------
# AC2 -- the candle-path statement names only the projected columns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac2_candle_repository_statements_name_only_candle_columns(
    recording_adapter,
):
    _seed(recording_adapter, [_row(1), _row(2)])
    repo = CandleRepository(mysql_adapter=recording_adapter, mongodb_adapter=None)

    with (
        patch.object(
            candle_repository_module.constants, "CANDLE_DATABASE_TYPE", "mysql"
        ),
        patch.object(
            candle_repository_module.constants, "CANDLE_READ_FALLBACK_ENABLED", False
        ),
    ):
        ranged = await repo.get_range(
            "BTCUSDT", "1h", datetime(2026, 1, 1), datetime(2026, 1, 2)
        )
        latest = await repo.get_latest("BTCUSDT", "1h", limit=1)

    assert len(ranged) == 2
    assert len(latest) == 1
    captured = _read_statements(recording_adapter)
    assert len(captured) == 2
    for statement in captured:
        assert set(_selected(statement)) == set(MYSQL_CANDLE_COLUMNS)
        assert len(_selected(statement)) == len(MYSQL_CANDLE_COLUMNS) == 8
        in_sql = _select_clause_columns(statement)
        assert in_sql == set(MYSQL_CANDLE_COLUMNS)
        assert in_sql.isdisjoint(CANDLE_PATH_WASTE)


@pytest.mark.asyncio
async def test_ac2_warmup_statement_names_only_its_eleven_columns(recording_adapter):
    _seed(recording_adapter, [_row(1), _row(2)])
    mongo = Mock()
    config = BackfillConfig(min_candles=10, force=True, dry_run=True)

    result = await backfill_pair(recording_adapter, mongo, "BTCUSDT", "1h", config)

    assert result.source_rows == 2
    assert result.written == 2
    captured = _read_statements(recording_adapter)
    assert len(captured) == 1
    in_sql = _select_clause_columns(captured[0])
    assert in_sql == set(WARMUP_COLUMNS)
    assert len(WARMUP_COLUMNS) == 11
    assert in_sql.isdisjoint(WARMUP_PATH_WASTE)


# ---------------------------------------------------------------------------
# AC4 -- columns=[] falls back to the whole table with a WARNING
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("columns", [[], ["nope"]])
def test_ac4_empty_resolved_list_selects_whole_table_and_warns(
    recording_adapter, caplog, columns
):
    _seed(recording_adapter, [_row(1)])
    with caplog.at_level("WARNING", logger="data_manager.db.mysql_adapter"):
        rows = recording_adapter.query_latest(TABLE, "BTCUSDT", columns=columns)

    assert set(rows[0]) == ALL_KLINES_COLUMNS
    (statement,) = _read_statements(recording_adapter)
    assert set(_selected(statement)) == ALL_KLINES_COLUMNS
    warnings = [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and "No requested columns exist" in r.getMessage()
    ]
    assert len(warnings) == 1


# ---------------------------------------------------------------------------
# AC7 -- map_mysql_row output is identical for projected and full rows
# ---------------------------------------------------------------------------


def test_ac7_map_mysql_row_identical_for_projected_and_full_rows(recording_adapter):
    _seed(recording_adapter, [_row(1), _row(2), _row(3)])
    start, end = datetime(2026, 1, 1), datetime(2026, 1, 2)

    full_rows = recording_adapter.query_range(TABLE, start, end, "BTCUSDT")
    projected_rows = recording_adapter.query_range(
        TABLE, start, end, "BTCUSDT", columns=MYSQL_CANDLE_COLUMNS
    )

    assert len(full_rows) == len(projected_rows) == 3
    for full, projected in zip(full_rows, projected_rows, strict=True):
        assert set(full) == ALL_KLINES_COLUMNS
        assert set(projected) == set(MYSQL_CANDLE_COLUMNS)
        mapped_full = map_mysql_row(full)
        mapped_projected = map_mysql_row(projected)
        assert mapped_projected == mapped_full
        assert set(mapped_projected) == set(_MYSQL_COLUMN_MAP)
        assert all(value is not None for value in mapped_projected.values())


# ---------------------------------------------------------------------------
# AC8 -- /candles response shape is unchanged on projected MySQL rows
# ---------------------------------------------------------------------------


async def _call_get_candles(adapter) -> dict:
    manager = SimpleNamespace(mysql_adapter=adapter, mongodb_adapter=AsyncMock())
    with (
        patch.object(api_module, "db_manager", manager),
        patch.object(
            candle_repository_module.constants, "CANDLE_DATABASE_TYPE", "mysql"
        ),
        patch.object(
            candle_repository_module.constants, "CANDLE_READ_FALLBACK_ENABLED", False
        ),
    ):
        return await get_candles(
            pair="BTCUSDT",
            period="1h",
            start=datetime(2026, 1, 1),
            end=datetime(2026, 1, 2),
            limit=100,
            offset=0,
            sort_order="asc",
        )


@pytest.mark.asyncio
async def test_ac8_get_candles_response_shape_unchanged_on_projected_rows(
    recording_adapter,
):
    _seed(recording_adapter, [_row(1), _row(2)])
    real_query_range = recording_adapter.query_range
    returned_rows: list[dict] = []

    def spy(*args, **kwargs):
        rows = real_query_range(*args, **kwargs)
        returned_rows.extend(rows)
        return rows

    recording_adapter.query_range = spy
    projected = await _call_get_candles(recording_adapter)

    # The MySQL rows really were projected (not a mock that pre-filters).
    assert returned_rows
    assert all(set(r) == set(MYSQL_CANDLE_COLUMNS) for r in returned_rows)

    assert len(projected["data"]) == 2
    for candle in projected["data"]:
        assert set(candle) == {
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "quote_volume",
            "trades_count",
        }
        assert candle["quote_volume"] is None
        assert candle["trades_count"] is None
        assert candle["open"] not in (None, "None")
        assert candle["close"] not in (None, "None")

    # Pre-change behaviour: same route, whole-table rows.
    def full_table(*args, **kwargs):
        kwargs["columns"] = None
        return real_query_range(*args, **kwargs)

    recording_adapter.query_range = full_table
    unprojected = await _call_get_candles(recording_adapter)
    assert projected["data"] == unprojected["data"]
    assert projected["pagination"] == unprojected["pagination"]


# ---------------------------------------------------------------------------
# AC9 -- row_to_candle on the 11-column projection, open_time fallback
# ---------------------------------------------------------------------------


def test_ac9_row_to_candle_on_eleven_column_row_uses_open_time_fallback(
    recording_adapter,
):
    _seed(recording_adapter, [_row(7)])
    (row,) = recording_adapter.query_latest(TABLE, "BTCUSDT", 1, columns=WARMUP_COLUMNS)
    assert set(row) == set(WARMUP_COLUMNS)
    assert len(row) == 11

    expected_ts = datetime(2026, 1, 1, 7, tzinfo=UTC)
    for without_timestamp in (
        {**row, "timestamp": None},  # all 11 keys, timestamp null
        {k: v for k, v in row.items() if k != "timestamp"},  # key absent
    ):
        candle = row_to_candle(without_timestamp, "1h")
        assert candle is not None
        assert candle.timestamp == expected_ts
        assert candle.symbol == "BTCUSDT"
        assert candle.open == Decimal("107.5")
        assert candle.high == Decimal("117.25")
        assert candle.low == Decimal("97.125")
        assert candle.close == Decimal("112.75")
        assert candle.volume == Decimal("1007")
        assert candle.quote_volume == Decimal("2007")
        assert candle.trades_count == 307
        assert candle.timeframe == "1h"
        assert all(value is not None for value in candle.model_dump().values())


# ---------------------------------------------------------------------------
# AC10 -- generic API pushes field_list down
# ---------------------------------------------------------------------------


async def _generic_query(adapter, field_list):
    manager = SimpleNamespace(mysql_adapter=adapter, increment_query_count=Mock())
    with patch.object(api_module, "db_manager", manager):
        return await _execute_query_internal(
            "mysql",
            TABLE,
            {"symbol": "BTCUSDT"},
            {"timestamp": 1},
            10,
            0,
            field_list,
        )


@pytest.mark.asyncio
async def test_ac10a_generic_without_field_list_selects_whole_table(
    recording_adapter,
):
    _seed(recording_adapter, [_row(1), _row(2)])
    table = recording_adapter._get_table(TABLE)
    real = recording_adapter.find_paginated
    recording_adapter.find_paginated = Mock(side_effect=real)

    response = await _generic_query(recording_adapter, None)

    assert recording_adapter.find_paginated.call_args.kwargs.get("columns") is None
    (statement,) = _read_statements(recording_adapter)
    pre_change = (
        select(table)
        .where(and_(table.c.symbol == "BTCUSDT"))
        .order_by(table.c.timestamp.asc())
        .limit(10)
        .offset(0)
    )
    assert _sql(statement) == _sql(pre_change)
    assert all(set(r) == ALL_KLINES_COLUMNS for r in response["data"])


@pytest.mark.asyncio
async def test_ac10b_generic_field_list_projects_statement(recording_adapter):
    _seed(recording_adapter, [_row(1), _row(2)])

    response = await _generic_query(recording_adapter, ["symbol", "close_price"])

    (statement,) = _read_statements(recording_adapter)
    assert _selected(statement) == ["symbol", "close_price"]
    assert _select_clause_columns(statement) == {"symbol", "close_price"}
    assert [set(r) for r in response["data"]] == [{"symbol", "close_price"}] * 2


@pytest.mark.asyncio
async def test_ac10c_generic_pushdown_response_matches_post_fetch_filtering(
    recording_adapter,
):
    _seed(recording_adapter, [_row(1), _row(2), _row(3)])
    field_list = ["symbol", "timestamp", "close_price", "bogus"]

    pushed_down = await _generic_query(recording_adapter, field_list)

    # Pre-change behaviour: whole-table fetch, _apply_field_selection filters.
    real = recording_adapter.find_paginated

    def post_fetch_only(*args, **kwargs):
        kwargs.pop("columns", None)
        return real(*args, **kwargs)

    recording_adapter.find_paginated = post_fetch_only
    post_fetch = await _generic_query(recording_adapter, field_list)

    projected, full = _read_statements(recording_adapter)
    assert set(_selected(projected)) == {"symbol", "timestamp", "close_price"}
    assert set(_selected(full)) == ALL_KLINES_COLUMNS

    assert len(pushed_down["data"]) == 3
    assert pushed_down["data"] == post_fetch["data"]
    assert pushed_down["pagination"] == post_fetch["pagination"]
    pushed_down["metadata"].pop("timestamp")
    post_fetch["metadata"].pop("timestamp")
    assert pushed_down["metadata"] == post_fetch["metadata"]


# ---------------------------------------------------------------------------
# AC11 -- the count still reflects every matching row
# ---------------------------------------------------------------------------


def test_ac11_total_counts_full_filtered_set_with_projection_and_limit(
    recording_adapter,
):
    _seed(
        recording_adapter,
        [_row(1), _row(2), _row(3), _row(4), _row(1, symbol="ETHUSDT")],
    )

    rows, total = recording_adapter.find_paginated(
        TABLE,
        filter_dict={"symbol": "BTCUSDT"},
        limit=2,
        offset=1,
        columns=["symbol", "close_price"],
    )

    assert len(rows) == 2
    assert all(set(r) == {"symbol", "close_price"} for r in rows)
    assert total == 4

    rows, total = recording_adapter.find_paginated(
        TABLE, limit=1, columns=MYSQL_CANDLE_COLUMNS
    )
    assert len(rows) == 1
    assert total == 5


# ---------------------------------------------------------------------------
# AC12 -- every MySQL read on the klines read path passes a non-None columns=
# ---------------------------------------------------------------------------

_READ_METHODS = {"query_range", "query_latest", "find_paginated"}


def _mysql_read_calls(module) -> list[ast.Call]:
    """Return every call that reaches a MySQL read method in ``module``.

    ``MySQLAdapter`` read methods are synchronous and ``MongoDBAdapter``'s are
    coroutines, so an awaited direct call is the Mongo branch; a non-awaited
    direct call, or a method handed to ``asyncio.to_thread``, is MySQL.
    """
    tree = ast.parse(Path(inspect.getfile(module)).read_text())
    awaited = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Await)}
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in _READ_METHODS:
            if id(node) not in awaited:
                calls.append(node)
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == "to_thread"
            and node.args
            and isinstance(node.args[0], ast.Attribute)
            and node.args[0].attr in _READ_METHODS
        ):
            calls.append(node)
    return calls


def test_ac12_mysql_read_methods_sync_and_mongo_async():
    for name in _READ_METHODS:
        assert not inspect.iscoroutinefunction(getattr(MySQLAdapter, name))
        assert inspect.iscoroutinefunction(getattr(MongoDBAdapter, name))


@pytest.mark.parametrize(
    ("module", "expected_sites"),
    [(candle_repository_module, 4), (warmup_module, 1)],
)
def test_ac12_every_klines_mysql_read_passes_columns(module, expected_sites):
    calls = _mysql_read_calls(module)
    assert len(calls) == expected_sites, [c.lineno for c in calls]
    for call in calls:
        columns = [kw for kw in call.keywords if kw.arg == "columns"]
        assert columns, f"{module.__name__}:{call.lineno} has no columns="
        value = columns[0].value
        assert not (isinstance(value, ast.Constant) and value.value is None), (
            f"{module.__name__}:{call.lineno} passes columns=None"
        )
