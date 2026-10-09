"""Every kline written to MongoDB is also copied to MySQL (data-manager#526)."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import mysql as mysql_dialect

import data_manager
from data_manager.db.mysql_adapter import MySQLAdapter
from data_manager.db.repositories.candle_repository import CandleRepository
from data_manager.db.repositories.kline_persistence import (
    KLINE_NATURAL_KEY,
    KLINES_MYSQL_COPY,
    persist_klines,
)
from data_manager.db.write_result import WriteResult
from data_manager.maintenance.copy_mongo_klines_to_mysql import (
    _build_parser,
    copy_interval,
)
from data_manager.maintenance.klines_mysql_freshness import freshness_loop
from data_manager.models.market_data import Candle


def _candle(symbol="BTCUSDT", timeframe="1h", hour=0):
    return Candle(
        symbol=symbol,
        timestamp=datetime(2026, 1, 1, hour, tzinfo=UTC),
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("90"),
        close=Decimal("105"),
        volume=Decimal("1000"),
        timeframe=timeframe,
    )


def _copies(interval, outcome):
    return KLINES_MYSQL_COPY.labels(interval=interval, outcome=outcome)._value.get()


@pytest.mark.asyncio
async def test_a_backfill_write_produces_the_mysql_copy_and_is_counted():
    mongo = Mock()
    mongo.write = AsyncMock(return_value=WriteResult(inserted=2))
    mysql = Mock()
    mysql.write_batch.return_value = WriteResult(inserted=2)
    repo = CandleRepository(mysql_adapter=mysql, mongodb_adapter=mongo)
    before = _copies("1h", "success")

    with patch(
        "data_manager.db.repositories.candle_repository.constants.CANDLE_DATABASE_TYPE",
        "mongodb",
    ):
        inserted = await repo.insert_batch([_candle(hour=0), _candle(hour=1)])

    assert inserted == 2
    assert mongo.write.call_args.args[1] == "klines_1h"
    rows, table = mysql.write_batch.call_args.args
    assert table == "klines_h1"
    assert [row.symbol for row in rows] == ["BTCUSDT", "BTCUSDT"]
    assert mysql.write_batch.call_args.kwargs == {
        "insert_only": True,
        "natural_key": KLINE_NATURAL_KEY,
    }
    assert _copies("1h", "success") == before + 1


@pytest.mark.asyncio
async def test_a_single_backfill_insert_is_copied_too():
    mongo = Mock()
    mongo.write = AsyncMock(return_value=WriteResult(inserted=1))
    mysql = Mock()
    repo = CandleRepository(mysql_adapter=mysql, mongodb_adapter=mongo)

    with patch(
        "data_manager.db.repositories.candle_repository.constants.CANDLE_DATABASE_TYPE",
        "mongodb",
    ):
        assert await repo.insert(_candle(timeframe="1d")) is True

    assert mysql.write_batch.call_args.args[1] == "klines_d1"


@pytest.mark.asyncio
async def test_existing_backfill_key_is_counted_as_duplicate_and_copied():
    mongo = Mock()
    mongo.write = AsyncMock(return_value=WriteResult(duplicates=1))
    mysql = Mock()
    mysql.write_batch.return_value = WriteResult(inserted=1)

    result = await persist_klines(
        mongo, mysql, "1h", [(None, _candle())], overwrite=False, wait_for_copy=True
    )

    assert result.upserted == 0
    assert result.duplicates == 1
    mongo.write.assert_awaited_once()
    mysql.write_batch.assert_called_once()


@pytest.mark.asyncio
async def test_a_failed_mysql_copy_is_counted_and_never_fails_the_mongo_write():
    mongo = Mock()
    mongo.write = AsyncMock(return_value=WriteResult(inserted=1))
    mysql = Mock()
    mysql.write_batch.side_effect = RuntimeError("mysql down")
    before = _copies("1h", "error")

    result = await persist_klines(
        mongo, mysql, "1h", [(None, _candle())], overwrite=False, wait_for_copy=True
    )

    assert result.upserted == 1
    assert _copies("1h", "error") == before + 1


@pytest.mark.asyncio
async def test_the_kill_switch_and_a_missing_adapter_are_reported(monkeypatch):
    mongo = Mock()
    mongo.write = AsyncMock(return_value=WriteResult(inserted=1))
    mysql = Mock()
    monkeypatch.setenv("PETROSA_KLINES_MYSQL_COPY_ENABLED", "false")
    off = await persist_klines(mongo, mysql, "1h", [(None, _candle())], overwrite=False)
    assert off.mysql_copy == "disabled"
    mysql.write_batch.assert_not_called()

    monkeypatch.setenv("PETROSA_KLINES_MYSQL_COPY_ENABLED", "true")
    none = await persist_klines(mongo, None, "1h", [(None, _candle())], overwrite=False)
    assert none.mysql_copy == "unavailable"


def test_no_other_module_writes_klines_to_mongo():
    """The only Mongo kline writers are the shared function and the MySQL-primary mirror."""
    root = Path(data_manager.__file__).parent
    allowed = {
        root / "db/repositories/kline_persistence.py",
        root / "db/repositories/candle_repository.py",
        # Fills Mongo gaps FROM the MySQL rows: the data is already in MySQL, so nothing to copy back.
        root / "maintenance/fill_mongo_klines_from_mysql.py",
    }
    offenders = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if path not in allowed
        and (
            "candle_to_mongo_kline(" in path.read_text()
            or 'f"klines_{' in path.read_text()
            and ".bulk_write(" in path.read_text()
        )
    ]
    assert offenders == []
    repository = (root / "db/repositories/candle_repository.py").read_text()
    # The Mongo-primary branches of insert/insert_batch go through the shared path.
    assert repository.count("self._persist_klines(") == 2
    assert "self.mongodb.write(" not in repository


def test_the_klines_copy_is_insert_only_on_symbol_and_timestamp():
    adapter = MySQLAdapter("sqlite:///:memory:", role="serving")
    adapter.engine_options = {}
    adapter.connect()
    try:
        table = sa.Table(
            "klines_h1",
            adapter.metadata,
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("symbol", sa.String(20), nullable=False),
            sa.Column("timestamp", sa.DateTime, nullable=False),
            sa.Column("extracted_at", sa.DateTime),
        )
        adapter.tables["klines_h1"] = table
        statements: list = []

        def execute(statement, *args, **_kwargs):
            statements.append(statement)
            if len(statements) == 1:
                return MagicScalar(1)
            return SimpleNamespace(rowcount=2)

        conn = Mock()
        conn.execute.side_effect = execute
        conn.begin.return_value = Mock()
        engine = Mock()
        engine.connect.return_value.__enter__ = Mock(return_value=conn)
        engine.connect.return_value.__exit__ = Mock(return_value=False)
        rows = [
            SimpleNamespace(
                model_dump=lambda mode="python", hour=hour: {
                    "id": f"BTCUSDT_{hour}",
                    "symbol": "BTCUSDT",
                    "timestamp": datetime(2026, 1, 1, hour),
                    "extracted_at": datetime(2026, 1, 2),
                }
            )
            for hour in (0, 1, 2)
        ]

        with patch.object(adapter, "_ensure_connected", return_value=engine):
            result = adapter.write(
                rows, "klines_h1", insert_only=True, natural_key=KLINE_NATURAL_KEY
            )

        dialect = mysql_dialect.dialect()
        count_sql, insert_sql = (str(s.compile(dialect=dialect)) for s in statements)
        assert "(klines_h1.symbol, klines_h1.timestamp) IN" in count_sql
        assert "ON DUPLICATE KEY UPDATE symbol = klines_h1.symbol" in insert_sql
        assert "extracted_at = " not in insert_sql.split("ON DUPLICATE KEY UPDATE")[1]
        assert (result.inserted, result.duplicates) == (2, 1)
    finally:
        adapter.disconnect()


class MagicScalar:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


def test_the_freshness_loop_runs_on_the_leader_only():
    manager = SimpleNamespace(
        mongodb_adapter=SimpleNamespace(query_latest=AsyncMock(return_value=[])),
        mysql_adapter=Mock(),
    )

    async def run(leader):
        stop = asyncio.Event()
        task = asyncio.create_task(
            freshness_loop(
                lambda: manager, stop, interval_seconds=60, is_leader=lambda: leader
            )
        )
        await asyncio.sleep(0)
        stop.set()
        await task

    asyncio.run(run(False))
    manager.mongodb_adapter.query_latest.assert_not_called()
    asyncio.run(run(True))
    manager.mongodb_adapter.query_latest.assert_called()


def test_the_recovery_tool_takes_exactly_the_runbook_flags():
    args = _build_parser().parse_args(
        ["--since", "2026-10-02T00:00:00Z", "--interval", "1h", "--interval", "1d"]
    )
    assert args.interval == ["1h", "1d"]
    assert args.apply is False  # dry run unless --apply
    assert _build_parser().parse_args(["--since", "x", "--dry-run"]).apply is False
    assert _build_parser().parse_args(["--since", "x", "--apply"]).apply is True
    assert _build_parser().parse_args(["--since", "x"]).interval is None


def test_the_recovery_tool_copies_insert_only_and_reports_counts(capsys):
    document = {
        "symbol": "BTCUSDT",
        "timestamp": datetime(2026, 10, 3, tzinfo=UTC),
        "open_price": "1",
        "high_price": "2",
        "low_price": "1",
        "close_price": "2",
        "volume": "3",
    }
    mongo = SimpleNamespace(query_range=AsyncMock(return_value=[document]))
    mysql = Mock()
    mysql.write_batch.return_value = WriteResult(inserted=0, duplicates=1)

    asyncio.run(
        copy_interval(
            mongo,
            mysql,
            "1h",
            datetime(2026, 10, 2, tzinfo=UTC),
            datetime(2026, 10, 4, tzinfo=UTC),
            1000,
            True,
        )
    )

    assert mysql.write_batch.call_args.kwargs == {
        "insert_only": True,
        "natural_key": KLINE_NATURAL_KEY,
    }
    assert "inserted=0 duplicates=1 failed=0" in capsys.readouterr().out


@pytest.fixture
def generic_client(mock_db_manager):
    from fastapi.testclient import TestClient

    import data_manager.api.app as api_module

    mock_db_manager.mongodb_adapter = Mock()
    mock_db_manager.mongodb_adapter.write = AsyncMock(
        return_value=WriteResult(inserted=1)
    )
    mock_db_manager.mysql_adapter = Mock()
    mock_db_manager.mysql_adapter.write_batch.return_value = WriteResult(inserted=1)
    app = api_module.create_app()
    api_module.db_manager = mock_db_manager
    yield TestClient(app)
    api_module.db_manager = None


def test_a_generic_gateway_kline_insert_is_copied_to_mysql_too(generic_client):
    import data_manager.api.app as api_module

    response = generic_client.post(
        "/api/v1/mongodb/klines_1h",
        json={
            "data": {
                "symbol": "BTCUSDT",
                "timestamp": "2026-10-03T00:00:00Z",
                "open_price": "1",
                "high_price": "2",
                "low_price": "1",
                "close_price": "2",
                "volume": "3",
            }
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["mysql_copy"] == "scheduled"
    deadline = 0
    mysql = api_module.db_manager.mysql_adapter
    while not mysql.write_batch.called and deadline < 50:
        import time

        time.sleep(0.02)
        deadline += 1
    rows, table = mysql.write_batch.call_args.args
    assert table == "klines_h1"
    assert rows[0].symbol == "BTCUSDT"
    assert mysql.write_batch.call_args.kwargs["insert_only"] is True


def test_a_generic_insert_into_another_collection_gets_no_kline_copy(generic_client):
    response = generic_client.post(
        "/api/v1/mongodb/alerts_misc", json={"data": {"symbol": "BTCUSDT"}}
    )

    assert response.status_code == 200, response.text
    assert "mysql_copy" not in response.json()
