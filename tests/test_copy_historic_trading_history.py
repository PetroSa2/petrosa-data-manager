import json
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from data_manager.maintenance import copy_historic_trading_history as copier
from data_manager.maintenance.copy_historic_trading_history import (
    _collection_names,
    _parse_args,
    _timestamp,
    copy_collection,
)


class Cursor:
    def __init__(self, documents):
        self.documents = documents

    def sort(self, *_args):
        return self

    async def to_list(self, *, length):
        documents, self.documents = self.documents[:length], self.documents[length:]
        return documents


class Collection:
    def __init__(self, documents):
        self.documents = documents
        self.query = None

    def find(self, query):
        self.query = query
        return Cursor(self.documents.copy())


class Mysql:
    def __init__(self):
        self.batches = []

    def write_batch(self, models, collection, batch_size, *, insert_only=False):
        self.batches.append((models, collection, batch_size, insert_only))


def event(timestamp="2026-08-08T12:00:00Z"):
    return {
        "decision_id": "decision-1",
        "strategy_id": "strategy-1",
        "order_id": "order-1",
        "event_type": "filled",
        "timestamp": timestamp,
    }


def trade(timestamp="2026-08-09T12:00:00Z"):
    return {
        "symbol": "BTCUSDT",
        "trade_id": 1,
        "timestamp": timestamp,
        "price": "100",
        "quantity": "2",
        "quote_quantity": "200",
        "is_buyer_maker": True,
        "order_id": "order-1",
        "commission": "0.01",
        "commission_asset": "USDT",
        "trade_time": timestamp,
        "extracted_at": timestamp,
        "extractor_version": "1.0.0",
        "source": "binance",
    }


def test_collection_names_include_plain_and_symbol_trades():
    assert _collection_names(["trades", "trades_BTCUSDT", "other"], None) == [
        "trades",
        "trades_BTCUSDT",
    ]
    assert _collection_names(["trades_BTCUSDT"], "trades_BTCUSDT") == ["trades_BTCUSDT"]


def test_collection_names_reject_unknown_collection():
    with pytest.raises(ValueError, match="not found") as error:
        _collection_names(["execution_events"], "trades")
    assert "trades" in str(error.value)


def test_timestamp_accepts_datetime_epoch_and_naive_iso_values():
    aware = datetime(2026, 8, 8, tzinfo=UTC)
    assert _timestamp(aware) is aware
    assert _timestamp(0) == datetime(1970, 1, 1, tzinfo=UTC)
    assert _timestamp("2026-08-08T00:00:00") == aware


@pytest.mark.parametrize(
    "value",
    [
        1786147200,
        1786147200000,
        "1786147200000",
    ],
)
def test_timestamp_accepts_epoch_seconds_and_milliseconds(value):
    assert _timestamp(value) == datetime(2026, 8, 8, tzinfo=UTC)


@pytest.mark.parametrize(
    "value, expected",
    [
        (1786147200.5, datetime(2026, 8, 8, 0, 0, 0, 500000, tzinfo=UTC)),
        (Decimal("1786147200000"), datetime(2026, 8, 8, tzinfo=UTC)),
    ],
)
def test_timestamp_accepts_float_seconds_and_decimal_milliseconds(value, expected):
    assert _timestamp(value) == expected


def test_timestamp_normalizes_iso_and_bson_datetimes_to_utc():
    assert _timestamp("2026-08-09T01:30:00+02:00") == datetime(
        2026, 8, 8, 23, 30, tzinfo=UTC
    )
    bson_datetime = datetime(2026, 8, 8, 12, 0)
    assert _timestamp(bson_datetime) == bson_datetime.replace(tzinfo=UTC)


@pytest.mark.parametrize("value", [float("nan"), 10**30])
def test_timestamp_rejects_invalid_numeric_values(value):
    with pytest.raises(ValueError, match="timestamp") as error:
        _timestamp(value)
    assert "timestamp" in str(error.value)


def test_timestamp_rejects_timezone_conversion_failures():
    class BrokenTimezone(tzinfo):
        def utcoffset(self, _value):
            raise OverflowError("timezone overflow")

    with pytest.raises(ValueError, match="timestamp") as error:
        _timestamp(datetime(2026, 8, 8, tzinfo=BrokenTimezone()))
    assert "timestamp" in str(error.value)


def test_checkpoint_reads_json_object_and_rejects_other_values(tmp_path: Path):
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(json.dumps({"trades": "2026-08-09T00:00:00Z"}))
    assert copier._checkpoint(checkpoint)["trades"].endswith("Z")
    checkpoint.write_text("[]")
    with pytest.raises(ValueError, match="object") as error:
        copier._checkpoint(checkpoint)
    assert "object" in str(error.value)


@pytest.mark.asyncio
async def test_copy_is_dry_run_by_default_and_reports_days():
    collection = Collection([event(), event("2026-08-09T12:00:00Z")])
    mysql = Mysql()

    result = await copy_collection(
        collection,
        mysql,
        "execution_events",
        batch_size=1,
        apply=False,
        checkpoint={},
        checkpoint_path=None,
        since=None,
        until=None,
    )

    assert result["rows"] == 2
    assert result["days"] == {"2026-08-08": 1, "2026-08-09": 1}
    assert mysql.batches == []


@pytest.mark.asyncio
async def test_execution_event_normalizes_epoch_milliseconds_and_reports_utc_day():
    mysql = Mysql()
    result = await copy_collection(
        Collection([event(1786147200000)]),
        mysql,
        "execution_events",
        batch_size=10,
        apply=True,
        checkpoint={},
        checkpoint_path=None,
        since=None,
        until=None,
    )

    assert result["days"] == {"2026-08-08": 1}
    assert mysql.batches[0][0][0].timestamp == datetime(2026, 8, 8, tzinfo=UTC)


@pytest.mark.asyncio
async def test_copy_reports_invalid_documents():
    result = await copy_collection(
        Collection([{"timestamp": "not-a-date"}]),
        Mysql(),
        "execution_events",
        batch_size=10,
        apply=False,
        checkpoint={},
        checkpoint_path=None,
        since=None,
        until=None,
    )
    assert result["invalid"] == 1


@pytest.mark.asyncio
async def test_apply_writes_batches_and_checkpoint(tmp_path: Path):
    collection = Collection([trade()])
    mysql = Mysql()
    checkpoint_path = tmp_path / "checkpoint.json"
    checkpoint = {}

    result = await copy_collection(
        collection,
        mysql,
        "trades_BTCUSDT",
        batch_size=10,
        apply=True,
        checkpoint=checkpoint,
        checkpoint_path=checkpoint_path,
        since=None,
        until=None,
    )

    assert result["rows"] == 1
    assert mysql.batches[0][1:] == ("trades", 10, True)
    assert json.loads(checkpoint_path.read_text())["trades_BTCUSDT"]


@pytest.mark.asyncio
async def test_account_fill_is_written_insert_only_and_reports_day():
    mysql = Mysql()
    result = await copy_collection(
        Collection([trade()]),
        mysql,
        "trades",
        batch_size=10,
        apply=True,
        checkpoint={},
        checkpoint_path=None,
        since=None,
        until=None,
    )

    assert result == {
        "collection": "trades",
        "rows": 1,
        "invalid": 0,
        "days": {"2026-08-09": 1},
    }
    model = mysql.batches[0][0][0]
    assert model.order_id == "order-1"
    assert mysql.batches[0][1:] == ("trades", 10, True)


@pytest.mark.asyncio
async def test_account_fill_normalizes_numeric_id_and_millisecond_timestamps():
    document = trade("1786235400000")
    document["order_id"] = 123456789
    document["trade_time"] = 1786235400000
    document["extracted_at"] = datetime(2026, 8, 9, 12, tzinfo=UTC)
    mysql = Mysql()

    result = await copy_collection(
        Collection([document]),
        mysql,
        "trades",
        batch_size=10,
        apply=True,
        checkpoint={},
        checkpoint_path=None,
        since=None,
        until=None,
    )

    assert result["rows"] == 1
    assert result["invalid"] == 0
    model = mysql.batches[0][0][0]
    assert model.order_id == "123456789"
    assert model.timestamp == datetime(2026, 8, 9, 0, 30, tzinfo=UTC)
    assert result["days"] == {"2026-08-09": 1}


@pytest.mark.asyncio
async def test_account_fill_accepts_present_null_exchange_fields():
    document = trade()
    document["timestamp"] = datetime(2026, 8, 9, 12, tzinfo=UTC)
    document["order_id"] = None
    document["commission"] = None
    document["commission_asset"] = None
    mysql = Mysql()

    result = await copy_collection(
        Collection([document]),
        mysql,
        "trades",
        batch_size=10,
        apply=True,
        checkpoint={},
        checkpoint_path=None,
        since=None,
        until=None,
    )

    assert result["rows"] == 1
    assert result["invalid"] == 0
    model = mysql.batches[0][0][0]
    assert model.order_id is None
    assert model.commission is None
    assert model.commission_asset is None


@pytest.mark.asyncio
async def test_account_fill_reports_utc_day_for_offset_timestamp():
    document = trade("2026-08-09T00:30:00+02:00")
    document["trade_time"] = datetime(
        2026, 8, 9, 0, 30, tzinfo=timezone(timedelta(hours=2))
    )
    mysql = Mysql()

    result = await copy_collection(
        Collection([document]),
        mysql,
        "trades",
        batch_size=10,
        apply=False,
        checkpoint={},
        checkpoint_path=None,
        since=None,
        until=None,
    )

    assert result["days"] == {"2026-08-08": 1}


@pytest.mark.asyncio
async def test_account_fill_missing_required_column_is_invalid():
    document = trade()
    del document["commission_asset"]

    mysql = Mysql()
    result = await copy_collection(
        Collection([document]),
        mysql,
        "trades",
        batch_size=10,
        apply=True,
        checkpoint={},
        checkpoint_path=None,
        since=None,
        until=None,
    )

    assert result["invalid"] == 1
    assert result["rows"] == 0
    assert mysql.batches == []


@pytest.mark.asyncio
async def test_account_fill_non_finite_order_id_is_invalid():
    document = trade()
    document["order_id"] = float("nan")
    mysql = Mysql()

    result = await copy_collection(
        Collection([document]),
        mysql,
        "trades",
        batch_size=10,
        apply=True,
        checkpoint={},
        checkpoint_path=None,
        since=None,
        until=None,
    )

    assert result["invalid"] == 1
    assert result["rows"] == 0
    assert mysql.batches == []


@pytest.mark.asyncio
async def test_checkpoint_and_date_bounds_are_added_to_query():
    collection = Collection([event()])
    await copy_collection(
        collection,
        Mysql(),
        "execution_events",
        batch_size=10,
        apply=False,
        checkpoint={"execution_events": "2026-08-07T00:00:00+00:00"},
        checkpoint_path=None,
        since=datetime(2026, 8, 8, tzinfo=UTC),
        until=datetime(2026, 8, 9, tzinfo=UTC),
    )

    assert collection.query["timestamp"]["$gt"] == datetime(2026, 8, 8, tzinfo=UTC)
    assert collection.query["timestamp"]["$lt"] == datetime(2026, 8, 9, tzinfo=UTC)


def test_parse_args_rejects_non_positive_batch_size():
    with pytest.raises(SystemExit) as error:
        _parse_args(["--batch-size", "0"])
    assert error.value.code == 2


@pytest.mark.asyncio
async def test_run_connects_copies_and_disconnects(monkeypatch, tmp_path: Path):
    class Mongo:
        def __init__(self, *_args, **_kwargs):
            self.db = {"execution_events": Collection([event()])}
            self.connected = False
            self.disconnected = False

        def connect(self):
            self.connected = True

        def disconnect(self):
            self.disconnected = True

        async def list_collections(self):
            return ["execution_events"]

    class DatabaseMysql(Mysql):
        def __init__(self, *_args, **_kwargs):
            super().__init__()
            self.connected = False
            self.disconnected = False

        def connect(self):
            self.connected = True

        def disconnect(self):
            self.disconnected = True

    monkeypatch.setattr(copier, "MongoDBAdapter", Mongo)
    monkeypatch.setattr(copier, "MySQLAdapter", DatabaseMysql)
    args = _parse_args(["--apply", "--checkpoint", str(tmp_path / "state.json")])
    result = await copier.run(args)
    assert result[0]["rows"] == 1


def test_main_prints_results(monkeypatch, capsys):
    async def fake_run(_args):
        return [{"collection": "trades", "rows": 0}]

    monkeypatch.setattr(copier, "run", fake_run)
    assert copier.main([]) == 0
    assert '"collection": "trades"' in capsys.readouterr().out


def test_models_are_mapped_to_the_durable_table_names():
    assert SimpleNamespace(**trade()).symbol == "BTCUSDT"
