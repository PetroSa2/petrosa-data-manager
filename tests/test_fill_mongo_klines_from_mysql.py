"""The MySQL-to-Mongo daily gap fill is dry-run first and insert-only (petrosa-data-manager#536)."""

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from data_manager.db.write_result import WriteResult
from data_manager.maintenance import fill_mongo_klines_from_mysql as fill
from data_manager.maintenance.fill_mongo_klines_from_mysql import (
    _build_parser,
    _failed,
    _line,
    fill_symbol,
    plan_symbol,
)

START = datetime(2026, 7, 8, tzinfo=UTC)
END = datetime(2026, 7, 13, tzinfo=UTC)


def _row(symbol, day):
    return {
        "symbol": symbol,
        "timestamp": datetime(2026, 7, day),
        "open_price": Decimal("1"),
        "high_price": Decimal("2"),
        "low_price": Decimal("1"),
        "close_price": Decimal("2"),
        "volume": Decimal("3"),
        "quote_asset_volume": Decimal("6"),
        "number_of_trades": 4,
    }


def _stores(mysql_days, mongo_days, symbol="BCHUSDT"):
    mysql = Mock()
    mysql.query_range.side_effect = lambda table, start, end, sym, **kw: [
        _row(symbol, d) for d in mysql_days if start.day <= d <= end.day
    ]
    mongo = SimpleNamespace(
        query_range=AsyncMock(
            return_value=[
                {"timestamp": datetime(2026, 7, d, tzinfo=UTC)} for d in mongo_days
            ]
        ),
        write=AsyncMock(return_value=WriteResult(inserted=0)),
    )
    return mongo, mysql


@pytest.mark.asyncio
async def test_the_dry_run_plan_reports_the_days_each_store_is_missing():
    mongo, mysql = _stores(mysql_days=[8, 9, 10, 12], mongo_days=[8, 11, 12])

    plan = await plan_symbol(mongo, mysql, "BCHUSDT", START, END)

    assert plan["mysql_days"] == 4
    assert plan["mongo_days"] == 3
    assert plan["missing_in_mongo"] == [date(2026, 7, 9), date(2026, 7, 10)]
    assert plan["missing_in_mysql"] == [date(2026, 7, 11)]
    mongo.write.assert_not_called()


@pytest.mark.asyncio
async def test_apply_inserts_only_the_missing_days_from_mysql():
    mongo, mysql = _stores(mysql_days=[8, 9, 10, 12], mongo_days=[8, 12])
    mongo.write = AsyncMock(return_value=WriteResult(inserted=2))

    result = await fill_symbol(
        mongo, mysql, "BCHUSDT", [date(2026, 7, 9), date(2026, 7, 10)]
    )

    docs, collection = mongo.write.call_args.args
    assert collection == "klines_1d"
    assert sorted(doc.timestamp.day for doc in docs) == [9, 10]
    assert result == {"inserted": 2, "duplicates": 0, "failed": 0, "unmappable": 0}


@pytest.mark.asyncio
async def test_a_day_that_cannot_be_mapped_is_counted_not_hidden():
    mongo, mysql = _stores(mysql_days=[9], mongo_days=[])
    mongo.write = AsyncMock(return_value=WriteResult(inserted=1))

    result = await fill_symbol(
        mongo, mysql, "BCHUSDT", [date(2026, 7, 9), date(2026, 7, 10)]
    )

    assert result["unmappable"] == 1
    assert _failed([{"applied": result}]) is True


@pytest.mark.asyncio
async def test_nothing_missing_writes_nothing():
    mongo, mysql = _stores(mysql_days=[8], mongo_days=[8])

    result = await fill_symbol(mongo, mysql, "BCHUSDT", [])

    assert result["inserted"] == 0
    mongo.write.assert_not_called()


def test_the_tool_is_dry_run_unless_apply_is_given():
    parser = _build_parser()

    assert parser.parse_args(["--since", "2026-07-08"]).apply is False
    assert parser.parse_args(["--dry-run"]).apply is False
    assert parser.parse_args(["--apply"]).apply is True
    assert parser.parse_args(["--symbol", "BTCUSDT", "--symbol", "BCHUSDT"]).symbol == [
        "BTCUSDT",
        "BCHUSDT",
    ]


def test_the_report_line_names_the_days():
    line = _line(
        {
            "symbol": "BCHUSDT",
            "mysql_days": 85,
            "mongo_days": 80,
            "missing_in_mongo": [date(2026, 7, 9)],
            "missing_in_mysql": [],
            "applied": {"inserted": 1, "duplicates": 0, "failed": 0, "unmappable": 0},
        }
    )

    assert "BCHUSDT: mysql=85 mongo=80 missing_in_mongo=1 [2026-07-09]" in line
    assert "applied: inserted=1" in line


def test_main_prints_and_returns_nonzero_only_when_an_applied_fill_fails(
    monkeypatch, capsys
):
    async def fake_run(_args):
        return [
            {
                "symbol": "BCHUSDT",
                "mysql_days": 1,
                "mongo_days": 0,
                "missing_in_mongo": [date(2026, 7, 9)],
                "missing_in_mysql": [],
                "applied": {
                    "inserted": 0,
                    "duplicates": 0,
                    "failed": 1,
                    "unmappable": 0,
                },
            }
        ]

    monkeypatch.setattr(fill, "run", fake_run)

    assert fill.main(["--apply"]) == 1
    assert "BCHUSDT" in capsys.readouterr().out
    assert fill.main([]) == 0  # a dry run never fails the command
