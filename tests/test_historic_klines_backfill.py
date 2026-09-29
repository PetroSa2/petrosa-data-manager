from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from data_manager.maintenance.historic_klines_backfill import (
    BackfillConfig,
    _parse_args,
    run_backfill,
)
from data_manager.maintenance.klines_completeness import (
    completeness_ratio,
    update_completeness,
)

ROW = [
    0,
    "1",
    "2",
    "0.5",
    "1.5",
    "10",
    0,
    "15",
    2,
    "5",
    "7",
]


class FakeBinance:
    async def get_klines(self, symbol, timeframe, start, end, limit):
        del symbol, timeframe, start, end, limit
        return [ROW]


class FakeMySQL:
    def __init__(self):
        self.calls = []

    def write_batch(self, rows, table, batch_size):
        self.calls.append((rows, table, batch_size))
        return SimpleNamespace(inserted=len(rows))


@pytest.mark.asyncio
async def test_backfill_dry_run_does_not_write(tmp_path: Path):
    mysql = FakeMySQL()
    config = BackfillConfig(
        symbols=["BTCUSDT"],
        timeframes=["1h"],
        start=datetime(2026, 9, 24, tzinfo=UTC),
        end=datetime(2026, 9, 24, 1, tzinfo=UTC),
        checkpoint=tmp_path / "checkpoint.json",
    )

    result = await run_backfill(FakeBinance(), mysql, config)

    assert result["fetched"] == 1
    assert result["inserted"] == 0
    assert mysql.calls == []


@pytest.mark.asyncio
async def test_backfill_is_resumable_and_writes_idempotent_rows(tmp_path: Path):
    mysql = FakeMySQL()
    config = BackfillConfig(
        symbols=["BTCUSDT"],
        timeframes=["1h"],
        start=datetime(2026, 9, 24, tzinfo=UTC),
        end=datetime(2026, 9, 24, 1, tzinfo=UTC),
        dry_run=False,
        checkpoint=tmp_path / "checkpoint.json",
    )

    first = await run_backfill(FakeBinance(), mysql, config)
    second = await run_backfill(FakeBinance(), mysql, config)

    assert first["inserted"] == 1
    assert second["fetched"] == 0
    assert len(mysql.calls) == 1


def test_completeness_ratio_reports_gap():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(hours=10)

    assert completeness_ratio(9, start, end, "1h") == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_backfill_options_and_invalid_checkpoint(tmp_path: Path):
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text("not json")
    args = _parse_args(["--symbols", "BTCUSDT", "--timeframes", "1d"])

    assert args.symbols == "BTCUSDT"
    assert args.timeframes == "1d"
    assert checkpoint.read_text() == "not json"
    config = BackfillConfig(
        symbols=["BTCUSDT"],
        timeframes=["1h"],
        start=datetime(2026, 9, 24, tzinfo=UTC),
        end=datetime(2026, 9, 24, 1, tzinfo=UTC),
        checkpoint=checkpoint,
    )
    assert (await run_backfill(FakeBinance(), FakeMySQL(), config))["fetched"] == 1


def test_update_completeness_publishes_metric():
    class Mysql:
        def get_record_count(self, table, start, end, symbol):
            assert table == "klines_h1"
            assert symbol == "BTCUSDT"
            return 9

    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(hours=10)
    assert update_completeness(Mysql(), "BTCUSDT", "1h", start, end) == pytest.approx(
        0.9
    )
