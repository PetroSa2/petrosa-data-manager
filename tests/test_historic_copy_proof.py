import json
import sys
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from data_manager.maintenance import historic_copy_proof as proof
from data_manager.maintenance.historic_copy_proof import (
    _mongo_daily_counts,
    _mongo_timestamp_metadata,
    _mysql_daily_counts,
    is_proof_collection,
    prove_daily_copy,
)


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    async def to_list(self, *, length):
        return self.rows


class _Collection:
    def __init__(self, rows):
        self.rows = rows
        self.pipeline = None

    def aggregate(self, pipeline):
        self.pipeline = pipeline
        return _Cursor(self.rows)


class _Database:
    def __init__(self, rows):
        self.collection = _Collection(rows)

    def __getitem__(self, name):
        return self.collection


def test_proof_collection_selection_includes_plain_trades():
    assert is_proof_collection("trades")
    assert is_proof_collection("trades_BTCUSDT")
    assert is_proof_collection("execution_events")
    assert not is_proof_collection("tradesome")


def test_daily_copy_proof_requires_mysql_to_cover_each_day():
    now = datetime(2026, 10, 10, 12, tzinfo=UTC)
    day = date(2026, 10, 8)
    result = prove_daily_copy(
        "trades",
        {day: 5},
        {day: 4},
        now=now,
        retention_days=1,
        copy_lag=timedelta(days=2),
    )

    assert result.proven is False
    assert result.failures == ("2026-10-08: mongo=5 mysql=4",)


def test_daily_copy_proof_passes_when_mysql_is_complete():
    now = datetime(2026, 10, 10, 12, tzinfo=UTC)
    day = date(2026, 10, 8)
    result = prove_daily_copy(
        "execution_events",
        {day: 4},
        {day: 4},
        now=now,
        retention_days=1,
        copy_lag=timedelta(days=2),
    )

    assert result.proven is True


def test_daily_copy_proof_checks_from_oldest_mongo_document_to_cutoff():
    now = datetime(2026, 10, 10, 12, tzinfo=UTC)
    result = prove_daily_copy(
        "trades",
        {date(2026, 10, 1): 2, date(2026, 10, 8): 1},
        {date(2026, 10, 1): 2, date(2026, 10, 8): 1},
        now=now,
        retention_days=1,
        copy_lag=timedelta(days=2),
        oldest_mongo_timestamp=datetime(2026, 10, 1, tzinfo=UTC),
    )

    assert result.proven is True
    assert result.checked_days[0] == date(2026, 10, 1)
    assert result.checked_days[-1] == date(2026, 10, 8)
    assert len(result.checked_days) == 8


def test_daily_copy_proof_fails_closed_for_invalid_timestamps():
    result = prove_daily_copy(
        "trades",
        {date(2026, 10, 8): 1},
        {date(2026, 10, 8): 1},
        now=datetime(2026, 10, 10, 12, tzinfo=UTC),
        retention_days=1,
        copy_lag=timedelta(days=2),
        oldest_mongo_timestamp=datetime(2026, 10, 8, tzinfo=UTC),
        invalid_timestamp_count=1,
    )

    assert result.proven is False
    assert result.failures == ("invalid timestamps: 1",)


@pytest.mark.asyncio
async def test_mongo_counts_normalize_string_timestamps():
    database = _Database([{"_id": "2026-10-08", "count": 2}])

    counts = await _mongo_daily_counts(
        database,
        "trades",
        start=datetime(2026, 10, 8, tzinfo=UTC),
        end=datetime(2026, 10, 9, tzinfo=UTC),
    )

    assert counts == {date(2026, 10, 8): 2}
    assert (
        database.collection.pipeline[0]["$project"]["normalized"]["$convert"]["to"]
        == "date"
    )


@pytest.mark.asyncio
async def test_mongo_metadata_reports_invalid_timestamps():
    database = _Database([{"oldest": datetime(2026, 10, 8, tzinfo=UTC), "invalid": 1}])

    oldest, invalid = await _mongo_timestamp_metadata(database, "trades")

    assert oldest == datetime(2026, 10, 8, tzinfo=UTC)
    assert invalid == 1


@pytest.mark.asyncio
async def test_mongo_metadata_returns_no_usable_timestamp_for_empty_collection():
    oldest, invalid = await _mongo_timestamp_metadata(_Database([]), "trades")

    assert oldest is None
    assert invalid == 0


# ---- input validation and the helpers around the proof ------------------------------------------


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"retention_days": 0, "copy_lag": timedelta(0)}, "retention_days"),
        ({"retention_days": 1, "copy_lag": timedelta(seconds=-1)}, "copy_lag"),
        (
            {
                "retention_days": 1,
                "copy_lag": timedelta(0),
                "invalid_timestamp_count": -1,
            },
            "invalid_timestamp_count",
        ),
    ],
)
def test_prove_daily_copy_rejects_invalid_arguments(kwargs, message):
    with pytest.raises(ValueError) as excinfo:
        prove_daily_copy("trades", {}, {}, **kwargs)
    assert message in str(excinfo.value)


@pytest.mark.asyncio
async def test_timestamp_metadata_without_rows_has_no_oldest_timestamp():
    assert await _mongo_timestamp_metadata(_Database([]), "trades") == (None, 0)


@pytest.mark.asyncio
async def test_timestamp_metadata_treats_a_naive_oldest_timestamp_as_utc():
    database = _Database([{"oldest": datetime(2026, 8, 8, 1, 2), "invalid": 3}])

    oldest, invalid = await _mongo_timestamp_metadata(database, "execution_events")

    assert oldest == datetime(2026, 8, 8, 1, 2, tzinfo=UTC)
    assert invalid == 3


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self.rows


class _Connection:
    def __init__(self, rows):
        self.rows = rows
        self.sql = None
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params):
        self.sql = str(query)
        self.params = params
        return _Result(self.rows)


class _Engine:
    def __init__(self, rows):
        self.connection = _Connection(rows)

    def connect(self):
        return self.connection


def test_mysql_daily_counts_groups_rows_by_day():
    adapter = SimpleNamespace(
        engine=_Engine(
            [
                {"day": datetime(2026, 8, 8, 0, 0), "count": 2},
                {"day": date(2026, 8, 9), "count": "5"},
            ]
        )
    )
    start = datetime(2026, 8, 8, tzinfo=UTC)
    end = datetime(2026, 8, 10, tzinfo=UTC)

    counts = _mysql_daily_counts(adapter, "trades", start=start, end=end)

    assert counts == {date(2026, 8, 8): 2, date(2026, 8, 9): 5}
    assert "FROM `trades`" in adapter.engine.connection.sql
    assert adapter.engine.connection.params == {"start": start, "end": end}


def test_mysql_daily_counts_requires_a_connection():
    with pytest.raises(RuntimeError) as excinfo:
        _mysql_daily_counts(
            SimpleNamespace(engine=None),
            "trades",
            start=datetime(2026, 8, 8, tzinfo=UTC),
            end=datetime(2026, 8, 9, tzinfo=UTC),
        )
    assert "not connected" in str(excinfo.value)


# ---- the command line --------------------------------------------------------------------------


class _FakeMongo:
    collections: list[str] = []
    instances: list["_FakeMongo"] = []

    def __init__(self, url, database_name=None):
        self.database_name = database_name
        self.db = object()
        self.connected = self.disconnected = False
        _FakeMongo.instances.append(self)

    def connect(self):
        self.connected = True

    def disconnect(self):
        self.disconnected = True

    async def list_collections(self):
        return list(_FakeMongo.collections)


class _FakeMySQL:
    instances: list["_FakeMySQL"] = []

    def __init__(self, uri):
        self.connected = self.disconnected = False
        _FakeMySQL.instances.append(self)

    def connect(self):
        self.connected = True

    def disconnect(self):
        self.disconnected = True


@pytest.fixture
def cli(monkeypatch):
    _FakeMongo.collections = []
    _FakeMongo.instances = []
    _FakeMySQL.instances = []
    monkeypatch.setattr(proof, "MongoDBAdapter", _FakeMongo)
    monkeypatch.setattr(proof, "MySQLAdapter", _FakeMySQL)
    monkeypatch.setattr(
        proof.constants, "CANDLE_MONGO_DATABASE", "operational", raising=False
    )
    return SimpleNamespace(mongo=_FakeMongo, mysql=_FakeMySQL)


def _stub_data(monkeypatch, *, oldest, invalid=0, mongo=None, mysql=None):
    async def metadata(database, collection):
        return oldest, invalid

    async def daily(database, collection, *, start, end):
        return dict(mongo or {})

    monkeypatch.setattr(proof, "_mongo_timestamp_metadata", metadata)
    monkeypatch.setattr(proof, "_mongo_daily_counts", daily)
    monkeypatch.setattr(
        proof, "_mysql_daily_counts", lambda adapter, table, *, start, end: mysql or {}
    )


@pytest.mark.asyncio
async def test_cli_proves_execution_events_and_trades_together(
    cli, monkeypatch, capsys
):
    cli.mongo.collections = ["execution_events", "trades_BTCUSDT", "other"]
    oldest = datetime.now(UTC) - timedelta(days=5)
    day = oldest.date()
    _stub_data(
        monkeypatch,
        oldest=oldest,
        mongo={day: 3},
        mysql={day: 3},
    )

    await proof._run_cli(None, retention_days=1, copy_lag_seconds=0)

    results = json.loads(capsys.readouterr().out)
    assert [item["collection"] for item in results] == ["execution_events", "trades"]
    assert all(item["failures"] == [] for item in results)
    assert cli.mongo.instances[0].database_name == "operational"
    assert cli.mongo.instances[0].connected and cli.mongo.instances[0].disconnected
    assert cli.mysql.instances[0].connected and cli.mysql.instances[0].disconnected


@pytest.mark.asyncio
async def test_cli_reports_a_day_the_copy_does_not_cover(cli, monkeypatch, capsys):
    cli.mongo.collections = ["execution_events"]
    oldest = datetime.now(UTC) - timedelta(days=5)
    day = oldest.date()
    _stub_data(monkeypatch, oldest=oldest, mongo={day: 4}, mysql={day: 1})

    await proof._run_cli("execution_events", retention_days=1, copy_lag_seconds=0)

    (result,) = json.loads(capsys.readouterr().out)
    assert result["collection"] == "execution_events"
    assert result["failures"] == [f"{day.isoformat()}: mongo=4 mysql=1"]


@pytest.mark.asyncio
async def test_cli_checks_one_named_trades_collection(cli, monkeypatch, capsys):
    cli.mongo.collections = ["trades_BTCUSDT", "trades_ETHUSDT"]
    oldest = datetime.now(UTC) - timedelta(days=5)
    _stub_data(monkeypatch, oldest=oldest, invalid=2)

    await proof._run_cli("trades_BTCUSDT", retention_days=1, copy_lag_seconds=0)

    (result,) = json.loads(capsys.readouterr().out)
    assert result["collection"] == "trades_BTCUSDT"
    assert result["invalid_timestamp_count"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "collections, requested, oldest, error, message",
    [
        (["execution_events"], "trades_BTCUSDT", None, ValueError, "not found"),
        (["other"], None, None, RuntimeError, "no historic-copy proof collections"),
        (["trades"], None, None, RuntimeError, "no usable Mongo timestamps"),
    ],
)
async def test_cli_stops_on_missing_input_and_still_disconnects(
    cli, monkeypatch, collections, requested, oldest, error, message
):
    cli.mongo.collections = collections
    _stub_data(monkeypatch, oldest=oldest)

    with pytest.raises(error, match=message):
        await proof._run_cli(requested, retention_days=1, copy_lag_seconds=0)

    assert cli.mongo.instances[0].disconnected
    assert cli.mysql.instances[0].disconnected


def test_main_passes_the_command_line_to_the_proof(monkeypatch):
    seen = {}

    async def fake_run(collection, retention_days, copy_lag_seconds):
        seen.update(
            collection=collection,
            retention_days=retention_days,
            copy_lag_seconds=copy_lag_seconds,
        )

    monkeypatch.setattr(proof, "_run_cli", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "proof",
            "--collection",
            "trades",
            "--retention-days",
            "3",
            "--copy-lag-seconds",
            "9",
        ],
    )

    proof.main()

    assert seen == {"collection": "trades", "retention_days": 3, "copy_lag_seconds": 9}
