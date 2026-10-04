from datetime import UTC, date, datetime, timedelta

import pytest

from data_manager.maintenance.historic_copy_proof import (
    _mongo_daily_counts,
    _mongo_timestamp_metadata,
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
