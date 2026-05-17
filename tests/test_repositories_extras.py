"""
Coverage for the remaining repositories: audit, backfill, catalog, depth, funding.
Same mocking pattern as test_repositories.py — asserts collection routing,
duplicate-key suppression, and error-swallowing behavior.
"""

from datetime import UTC, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest

from data_manager.db.repositories.audit_repository import AuditRepository
from data_manager.db.repositories.backfill_repository import BackfillRepository
from data_manager.db.repositories.catalog_repository import CatalogRepository
from data_manager.db.repositories.depth_repository import DepthRepository
from data_manager.db.repositories.funding_repository import FundingRepository
from data_manager.models.market_data import FundingRate, OrderBookDepth, OrderBookLevel


def make_funding(symbol: str = "BTCUSDT") -> FundingRate:
    return FundingRate(
        symbol=symbol,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        funding_rate=Decimal("0.0001"),
    )


def make_depth(symbol: str = "BTCUSDT") -> OrderBookDepth:
    return OrderBookDepth(
        symbol=symbol,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        bids=[OrderBookLevel(price=Decimal("100"), quantity=Decimal("1"))],
        asks=[OrderBookLevel(price=Decimal("101"), quantity=Decimal("1"))],
    )


class TestAuditRepository:
    @pytest.mark.asyncio
    async def test_log_gap_writes_to_audit_logs_table(self):
        mysql = Mock()
        mysql.write = Mock()
        repo = AuditRepository(mysql_adapter=mysql, mongodb_adapter=None)
        ok = await repo.log_gap(
            "ds-1",
            "BTCUSDT",
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 1, 1, tzinfo=UTC),
            severity="high",
        )
        assert ok is True
        assert mysql.write.call_args[0][1] == "audit_logs"
        record = mysql.write.call_args[0][0][0].model_dump()
        assert record["audit_type"] == "gap"
        assert record["severity"] == "high"
        assert record["dataset_id"] == "ds-1"
        assert "audit_id" in record

    @pytest.mark.asyncio
    async def test_log_gap_returns_false_on_exception(self):
        mysql = Mock()
        mysql.write = Mock(side_effect=RuntimeError("write failed"))
        repo = AuditRepository(mysql_adapter=mysql, mongodb_adapter=None)
        ok = await repo.log_gap(
            "ds-1",
            "BTCUSDT",
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 1, 1, tzinfo=UTC),
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_log_health_check_records_info_by_default(self):
        mysql = Mock()
        mysql.write = Mock()
        repo = AuditRepository(mysql_adapter=mysql, mongodb_adapter=None)
        ok = await repo.log_health_check("ds-1", "BTCUSDT", "all green")
        assert ok is True
        record = mysql.write.call_args[0][0][0].model_dump()
        assert record["audit_type"] == "health_check"
        assert record["severity"] == "info"
        assert record["details"] == "all green"

    @pytest.mark.asyncio
    async def test_log_health_check_returns_false_on_exception(self):
        mysql = Mock()
        mysql.write = Mock(side_effect=RuntimeError("x"))
        repo = AuditRepository(mysql_adapter=mysql, mongodb_adapter=None)
        assert await repo.log_health_check("ds-1", "BTCUSDT", "any") is False

    def test_get_recent_logs_passes_through(self):
        mysql = Mock()
        mysql.query_latest = Mock(return_value=[{"audit_id": "1"}])
        repo = AuditRepository(mysql_adapter=mysql, mongodb_adapter=None)
        assert repo.get_recent_logs("ds-1", limit=50) == [{"audit_id": "1"}]
        mysql.query_latest.assert_called_once_with(
            "audit_logs", symbol="ds-1", limit=50
        )

    def test_get_recent_logs_returns_empty_on_exception(self):
        mysql = Mock()
        mysql.query_latest = Mock(side_effect=RuntimeError("x"))
        repo = AuditRepository(mysql_adapter=mysql, mongodb_adapter=None)
        assert repo.get_recent_logs() == []


class TestBackfillRepository:
    @pytest.mark.asyncio
    async def test_create_job_writes_to_backfill_jobs(self):
        mysql = Mock()
        mysql.write = Mock()
        repo = BackfillRepository(mysql_adapter=mysql, mongodb_adapter=None)
        job = {"job_id": "j-1", "status": "queued"}
        assert await repo.create_job(job) is True
        assert mysql.write.call_args[0][1] == "backfill_jobs"

    @pytest.mark.asyncio
    async def test_create_job_returns_false_on_exception(self):
        mysql = Mock()
        mysql.write = Mock(side_effect=RuntimeError("x"))
        repo = BackfillRepository(mysql_adapter=mysql, mongodb_adapter=None)
        assert await repo.create_job({"job_id": "j-1"}) is False

    def test_get_job_finds_matching_job_id(self):
        mysql = Mock()
        mysql.query_latest = Mock(
            return_value=[
                {"job_id": "j-1", "status": "queued"},
                {"job_id": "j-2", "status": "running"},
            ]
        )
        repo = BackfillRepository(mysql_adapter=mysql, mongodb_adapter=None)
        assert repo.get_job("j-2") == {"job_id": "j-2", "status": "running"}

    def test_get_job_returns_none_when_not_found(self):
        mysql = Mock()
        mysql.query_latest = Mock(return_value=[{"job_id": "j-1"}])
        repo = BackfillRepository(mysql_adapter=mysql, mongodb_adapter=None)
        assert repo.get_job("missing") is None

    def test_get_job_returns_none_on_exception(self):
        mysql = Mock()
        mysql.query_latest = Mock(side_effect=RuntimeError("x"))
        repo = BackfillRepository(mysql_adapter=mysql, mongodb_adapter=None)
        assert repo.get_job("anything") is None

    @pytest.mark.asyncio
    async def test_update_status_returns_true(self):
        # Current implementation is a stub but exercises the happy path.
        repo = BackfillRepository(mysql_adapter=Mock(), mongodb_adapter=None)
        assert await repo.update_status("j-1", "completed") is True

    @pytest.mark.asyncio
    async def test_update_status_with_error(self):
        repo = BackfillRepository(mysql_adapter=Mock(), mongodb_adapter=None)
        assert (
            await repo.update_status("j-1", "failed", error="connection refused")
            is True
        )


class TestCatalogRepository:
    @pytest.mark.asyncio
    async def test_upsert_dataset_writes_to_datasets(self):
        mysql = Mock()
        mysql.write = Mock()
        repo = CatalogRepository(mysql_adapter=mysql, mongodb_adapter=None)
        ok = await repo.upsert_dataset({"dataset_id": "ds-1", "name": "trades"})
        assert ok is True
        assert mysql.write.call_args[0][1] == "datasets"

    @pytest.mark.asyncio
    async def test_upsert_dataset_returns_false_on_exception(self):
        mysql = Mock()
        mysql.write = Mock(side_effect=RuntimeError("x"))
        repo = CatalogRepository(mysql_adapter=mysql, mongodb_adapter=None)
        assert await repo.upsert_dataset({"dataset_id": "ds-1"}) is False

    def test_get_all_datasets_passes_through(self):
        mysql = Mock()
        mysql.query_latest = Mock(return_value=[{"dataset_id": "ds-1"}])
        repo = CatalogRepository(mysql_adapter=mysql, mongodb_adapter=None)
        assert repo.get_all_datasets() == [{"dataset_id": "ds-1"}]

    def test_get_all_datasets_returns_empty_on_exception(self):
        mysql = Mock()
        mysql.query_latest = Mock(side_effect=RuntimeError("x"))
        repo = CatalogRepository(mysql_adapter=mysql, mongodb_adapter=None)
        assert repo.get_all_datasets() == []

    def test_get_dataset_finds_match(self):
        mysql = Mock()
        mysql.query_latest = Mock(
            return_value=[{"dataset_id": "ds-1"}, {"dataset_id": "ds-2"}]
        )
        repo = CatalogRepository(mysql_adapter=mysql, mongodb_adapter=None)
        assert repo.get_dataset("ds-2") == {"dataset_id": "ds-2"}

    def test_get_dataset_returns_none_when_missing(self):
        mysql = Mock()
        mysql.query_latest = Mock(return_value=[{"dataset_id": "ds-1"}])
        repo = CatalogRepository(mysql_adapter=mysql, mongodb_adapter=None)
        assert repo.get_dataset("missing") is None

    def test_get_dataset_returns_none_on_exception(self):
        mysql = Mock()
        # get_dataset calls get_all_datasets which raises — outer try catches.
        mysql.query_latest = Mock(side_effect=RuntimeError("x"))
        repo = CatalogRepository(mysql_adapter=mysql, mongodb_adapter=None)
        # get_all_datasets swallows and returns [] so the loop returns None.
        assert repo.get_dataset("any") is None


class TestDepthRepository:
    @pytest.mark.asyncio
    async def test_insert_writes_to_per_symbol_collection(self):
        mongodb = Mock()
        mongodb.write = AsyncMock(return_value=1)
        repo = DepthRepository(mysql_adapter=None, mongodb_adapter=mongodb)
        assert await repo.insert(make_depth("BTCUSDT")) is True
        assert mongodb.write.call_args[0][1] == "depth_BTCUSDT"

    @pytest.mark.asyncio
    async def test_insert_returns_false_when_no_records(self):
        mongodb = Mock()
        mongodb.write = AsyncMock(return_value=0)
        repo = DepthRepository(mysql_adapter=None, mongodb_adapter=mongodb)
        assert await repo.insert(make_depth()) is False

    @pytest.mark.asyncio
    async def test_insert_returns_false_on_generic_exception(self):
        mongodb = Mock()
        mongodb.write = AsyncMock(side_effect=RuntimeError("mongo down"))
        repo = DepthRepository(mysql_adapter=None, mongodb_adapter=mongodb)
        assert await repo.insert(make_depth()) is False

    @pytest.mark.asyncio
    async def test_insert_treats_duplicate_key_as_skip(self):
        mongodb = Mock()
        mongodb.write = AsyncMock(side_effect=Exception("E11000 duplicate key error"))
        repo = DepthRepository(mysql_adapter=None, mongodb_adapter=mongodb)
        # Duplicate-key path returns False but is logged at debug, not error.
        assert await repo.insert(make_depth()) is False

    @pytest.mark.asyncio
    async def test_insert_batch_empty_returns_zero(self):
        repo = DepthRepository(mysql_adapter=None, mongodb_adapter=Mock())
        assert await repo.insert_batch([]) == 0

    @pytest.mark.asyncio
    async def test_insert_batch_groups_by_symbol(self):
        mongodb = Mock()
        mongodb.write = AsyncMock(side_effect=[2, 1])
        repo = DepthRepository(mysql_adapter=None, mongodb_adapter=mongodb)
        total = await repo.insert_batch(
            [make_depth("BTCUSDT"), make_depth("BTCUSDT"), make_depth("ETHUSDT")]
        )
        assert total == 3
        assert mongodb.write.call_count == 2

    @pytest.mark.asyncio
    async def test_insert_batch_returns_zero_on_exception(self):
        mongodb = Mock()
        mongodb.write = AsyncMock(side_effect=RuntimeError("boom"))
        repo = DepthRepository(mysql_adapter=None, mongodb_adapter=mongodb)
        assert await repo.insert_batch([make_depth()]) == 0

    @pytest.mark.asyncio
    async def test_get_latest_passes_through(self):
        mongodb = Mock()
        mongodb.query_latest = AsyncMock(return_value=[{"symbol": "BTCUSDT"}])
        repo = DepthRepository(mysql_adapter=None, mongodb_adapter=mongodb)
        assert await repo.get_latest("BTCUSDT", limit=2) == [{"symbol": "BTCUSDT"}]
        mongodb.query_latest.assert_called_once_with("depth_BTCUSDT", "BTCUSDT", 2)

    @pytest.mark.asyncio
    async def test_get_latest_returns_empty_on_exception(self):
        mongodb = Mock()
        mongodb.query_latest = AsyncMock(side_effect=RuntimeError("x"))
        repo = DepthRepository(mysql_adapter=None, mongodb_adapter=mongodb)
        assert await repo.get_latest("BTCUSDT") == []


class TestFundingRepository:
    @pytest.mark.asyncio
    async def test_insert_writes_to_per_symbol_collection(self):
        mongodb = Mock()
        mongodb.write = AsyncMock(return_value=1)
        repo = FundingRepository(mysql_adapter=None, mongodb_adapter=mongodb)
        assert await repo.insert(make_funding("BTCUSDT")) is True
        assert mongodb.write.call_args[0][1] == "funding_rates_BTCUSDT"

    @pytest.mark.asyncio
    async def test_insert_returns_false_when_no_records(self):
        mongodb = Mock()
        mongodb.write = AsyncMock(return_value=0)
        repo = FundingRepository(mysql_adapter=None, mongodb_adapter=mongodb)
        assert await repo.insert(make_funding()) is False

    @pytest.mark.asyncio
    async def test_insert_returns_false_on_exception(self):
        mongodb = Mock()
        mongodb.write = AsyncMock(side_effect=RuntimeError("boom"))
        repo = FundingRepository(mysql_adapter=None, mongodb_adapter=mongodb)
        assert await repo.insert(make_funding()) is False

    @pytest.mark.asyncio
    async def test_batch_empty_returns_zero(self):
        repo = FundingRepository(mysql_adapter=None, mongodb_adapter=Mock())
        assert await repo.insert_batch([]) == 0

    @pytest.mark.asyncio
    async def test_batch_groups_by_symbol(self):
        mongodb = Mock()
        mongodb.write = AsyncMock(side_effect=[2, 1])
        repo = FundingRepository(mysql_adapter=None, mongodb_adapter=mongodb)
        total = await repo.insert_batch(
            [make_funding("BTCUSDT"), make_funding("BTCUSDT"), make_funding("ETHUSDT")]
        )
        assert total == 3

    @pytest.mark.asyncio
    async def test_batch_returns_zero_on_exception(self):
        mongodb = Mock()
        mongodb.write = AsyncMock(side_effect=RuntimeError("boom"))
        repo = FundingRepository(mysql_adapter=None, mongodb_adapter=mongodb)
        assert await repo.insert_batch([make_funding()]) == 0

    @pytest.mark.asyncio
    async def test_get_range_passes_through(self):
        mongodb = Mock()
        mongodb.query_range = AsyncMock(return_value=[{"funding_rate": "0.0001"}])
        repo = FundingRepository(mysql_adapter=None, mongodb_adapter=mongodb)
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = datetime(2026, 1, 2, tzinfo=UTC)
        result = await repo.get_range("BTCUSDT", start, end)
        assert result == [{"funding_rate": "0.0001"}]
        mongodb.query_range.assert_called_once_with(
            "funding_rates_BTCUSDT", start, end, "BTCUSDT"
        )

    @pytest.mark.asyncio
    async def test_get_range_returns_empty_on_exception(self):
        mongodb = Mock()
        mongodb.query_range = AsyncMock(side_effect=RuntimeError("x"))
        repo = FundingRepository(mysql_adapter=None, mongodb_adapter=mongodb)
        assert (
            await repo.get_range(
                "BTCUSDT",
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2026, 1, 2, tzinfo=UTC),
            )
            == []
        )

    @pytest.mark.asyncio
    async def test_get_latest_passes_through(self):
        mongodb = Mock()
        mongodb.query_latest = AsyncMock(return_value=[{"funding_rate": "0.0001"}])
        repo = FundingRepository(mysql_adapter=None, mongodb_adapter=mongodb)
        assert await repo.get_latest("BTCUSDT", limit=3) == [{"funding_rate": "0.0001"}]

    @pytest.mark.asyncio
    async def test_get_latest_returns_empty_on_exception(self):
        mongodb = Mock()
        mongodb.query_latest = AsyncMock(side_effect=RuntimeError("x"))
        repo = FundingRepository(mysql_adapter=None, mongodb_adapter=mongodb)
        assert await repo.get_latest("BTCUSDT") == []
