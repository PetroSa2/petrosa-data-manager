"""
Unit tests for data_manager.db.repositories.* (health, ticker, trade).

Mocks the MongoDB/MySQL adapters; verifies the repositories correctly route
single/batch writes, group by symbol, propagate query results, and swallow
adapter exceptions per their documented contract.
"""

from datetime import UTC, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest

from data_manager.db.repositories.base_repository import BaseRepository
from data_manager.db.repositories.health_repository import HealthRepository
from data_manager.db.repositories.ticker_repository import TickerRepository
from data_manager.db.repositories.trade_repository import TradeRepository
from data_manager.models.health import DataHealthMetrics
from data_manager.models.market_data import Ticker, Trade


def make_ticker(symbol: str = "BTCUSDT") -> Ticker:
    return Ticker(
        symbol=symbol,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        open_price=Decimal("100.00"),
        high_price=Decimal("110.00"),
        low_price=Decimal("90.00"),
        close_price=Decimal("105.00"),
        volume=Decimal("1234.5"),
        quote_volume=Decimal("130000.00"),
        price_change=Decimal("5.00"),
        price_change_percent=Decimal("5.00"),
        trades_count=1000,
    )


def make_trade(symbol: str = "BTCUSDT", trade_id: int = 1) -> Trade:
    return Trade(
        symbol=symbol,
        trade_id=trade_id,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        price=Decimal("100.00"),
        quantity=Decimal("0.5"),
        quote_quantity=Decimal("50.00"),
        is_buyer_maker=True,
        side="buy",
    )


def make_health_metrics() -> DataHealthMetrics:
    return DataHealthMetrics(
        completeness=99.5,
        freshness_seconds=10,
        gaps_count=0,
        duplicates_count=0,
        consistency_score=98.0,
        quality_score=99.0,
    )


class TestBaseRepository:
    def test_model_to_dict_uses_model_dump(self):
        repo = BaseRepository(mysql_adapter=None, mongodb_adapter=None)
        ticker = make_ticker()
        out = repo._model_to_dict(ticker)
        assert out["symbol"] == "BTCUSDT"
        assert out["trades_count"] == 1000

    def test_model_to_dict_falls_back_to_dict_attr(self):
        repo = BaseRepository(mysql_adapter=None, mongodb_adapter=None)

        class LegacyModel:
            def dict(self):
                return {"legacy": True}

        assert repo._model_to_dict(LegacyModel()) == {"legacy": True}

    def test_model_to_dict_falls_back_to_dict_cast(self):
        repo = BaseRepository(mysql_adapter=None, mongodb_adapter=None)
        # Plain dict-like (no model_dump or dict method).
        plain = {"k": "v"}
        assert repo._model_to_dict(plain) == {"k": "v"}

    def test_models_to_dicts_maps_each_model(self):
        repo = BaseRepository(mysql_adapter=None, mongodb_adapter=None)
        t1 = make_ticker("BTCUSDT")
        t2 = make_ticker("ETHUSDT")
        out = repo._models_to_dicts([t1, t2])
        assert len(out) == 2
        assert out[0]["symbol"] == "BTCUSDT"
        assert out[1]["symbol"] == "ETHUSDT"


class TestTickerRepositoryInsert:
    @pytest.mark.asyncio
    async def test_insert_writes_to_per_symbol_collection(self):
        mongodb = Mock()
        mongodb.write = AsyncMock(return_value=1)
        repo = TickerRepository(mysql_adapter=None, mongodb_adapter=mongodb)
        ticker = make_ticker("BTCUSDT")
        assert await repo.insert(ticker) is True
        mongodb.write.assert_called_once()
        args = mongodb.write.call_args
        assert args[0][1] == "tickers_BTCUSDT"

    @pytest.mark.asyncio
    async def test_insert_returns_false_when_no_records_written(self):
        mongodb = Mock()
        mongodb.write = AsyncMock(return_value=0)
        repo = TickerRepository(mysql_adapter=None, mongodb_adapter=mongodb)
        assert await repo.insert(make_ticker()) is False

    @pytest.mark.asyncio
    async def test_insert_returns_false_on_adapter_exception(self):
        mongodb = Mock()
        mongodb.write = AsyncMock(side_effect=RuntimeError("mongo down"))
        repo = TickerRepository(mysql_adapter=None, mongodb_adapter=mongodb)
        assert await repo.insert(make_ticker()) is False


class TestTickerRepositoryBatch:
    @pytest.mark.asyncio
    async def test_empty_batch_returns_zero(self):
        repo = TickerRepository(mysql_adapter=None, mongodb_adapter=Mock())
        assert await repo.insert_batch([]) == 0

    @pytest.mark.asyncio
    async def test_groups_by_symbol_and_writes_each_collection(self):
        mongodb = Mock()
        mongodb.write = AsyncMock(side_effect=[2, 1])  # BTCUSDT then ETHUSDT
        repo = TickerRepository(mysql_adapter=None, mongodb_adapter=mongodb)

        tickers = [
            make_ticker("BTCUSDT"),
            make_ticker("BTCUSDT"),
            make_ticker("ETHUSDT"),
        ]
        total = await repo.insert_batch(tickers)
        assert total == 3
        assert mongodb.write.call_count == 2
        # First call: BTCUSDT collection, two tickers; second: ETHUSDT collection, one.
        first_call, second_call = mongodb.write.call_args_list
        first_symbols = {t.symbol for t in first_call[0][0]}
        second_symbols = {t.symbol for t in second_call[0][0]}
        # Order of dict iteration in Python 3.7+ is insertion order so BTCUSDT is first.
        assert first_symbols == {"BTCUSDT"}
        assert first_call[0][1] == "tickers_BTCUSDT"
        assert second_symbols == {"ETHUSDT"}
        assert second_call[0][1] == "tickers_ETHUSDT"

    @pytest.mark.asyncio
    async def test_batch_returns_zero_on_exception(self):
        mongodb = Mock()
        mongodb.write = AsyncMock(side_effect=RuntimeError("boom"))
        repo = TickerRepository(mysql_adapter=None, mongodb_adapter=mongodb)
        assert await repo.insert_batch([make_ticker()]) == 0


class TestTickerRepositoryQuery:
    @pytest.mark.asyncio
    async def test_get_latest_uses_per_symbol_collection(self):
        mongodb = Mock()
        mongodb.query_latest = AsyncMock(return_value=[{"symbol": "BTCUSDT"}])
        repo = TickerRepository(mysql_adapter=None, mongodb_adapter=mongodb)
        results = await repo.get_latest("BTCUSDT", limit=5)
        assert results == [{"symbol": "BTCUSDT"}]
        mongodb.query_latest.assert_called_once_with("tickers_BTCUSDT", "BTCUSDT", 5)

    @pytest.mark.asyncio
    async def test_get_latest_returns_empty_on_exception(self):
        mongodb = Mock()
        mongodb.query_latest = AsyncMock(side_effect=RuntimeError("boom"))
        repo = TickerRepository(mysql_adapter=None, mongodb_adapter=mongodb)
        assert await repo.get_latest("BTCUSDT") == []


class TestTradeRepositoryInsert:
    @pytest.mark.asyncio
    async def test_insert_writes_to_per_symbol_collection(self):
        mongodb = Mock()
        mongodb.write = AsyncMock(return_value=1)
        repo = TradeRepository(mysql_adapter=None, mongodb_adapter=mongodb)
        assert await repo.insert(make_trade("BTCUSDT")) is True
        assert mongodb.write.call_args[0][1] == "trades_BTCUSDT"

    @pytest.mark.asyncio
    async def test_insert_returns_false_when_no_records(self):
        mongodb = Mock()
        mongodb.write = AsyncMock(return_value=0)
        repo = TradeRepository(mysql_adapter=None, mongodb_adapter=mongodb)
        assert await repo.insert(make_trade()) is False

    @pytest.mark.asyncio
    async def test_insert_returns_false_on_exception(self):
        mongodb = Mock()
        mongodb.write = AsyncMock(side_effect=RuntimeError("x"))
        repo = TradeRepository(mysql_adapter=None, mongodb_adapter=mongodb)
        assert await repo.insert(make_trade()) is False


class TestTradeRepositoryBatch:
    @pytest.mark.asyncio
    async def test_empty_batch_returns_zero(self):
        repo = TradeRepository(mysql_adapter=None, mongodb_adapter=Mock())
        assert await repo.insert_batch([]) == 0

    @pytest.mark.asyncio
    async def test_batch_groups_by_symbol(self):
        mongodb = Mock()
        mongodb.write = AsyncMock(side_effect=[3, 2])
        repo = TradeRepository(mysql_adapter=None, mongodb_adapter=mongodb)
        total = await repo.insert_batch(
            [
                make_trade("BTCUSDT", 1),
                make_trade("BTCUSDT", 2),
                make_trade("BTCUSDT", 3),
                make_trade("ETHUSDT", 4),
                make_trade("ETHUSDT", 5),
            ]
        )
        assert total == 5
        assert mongodb.write.call_count == 2

    @pytest.mark.asyncio
    async def test_batch_returns_zero_on_exception(self):
        mongodb = Mock()
        mongodb.write = AsyncMock(side_effect=RuntimeError("boom"))
        repo = TradeRepository(mysql_adapter=None, mongodb_adapter=mongodb)
        assert await repo.insert_batch([make_trade()]) == 0


class TestTradeRepositoryQueries:
    @pytest.mark.asyncio
    async def test_get_range_uses_per_symbol_collection(self):
        mongodb = Mock()
        mongodb.query_range = AsyncMock(return_value=[{"trade_id": 1}])
        repo = TradeRepository(mysql_adapter=None, mongodb_adapter=mongodb)
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = datetime(2026, 1, 2, tzinfo=UTC)
        result = await repo.get_range("BTCUSDT", start, end)
        assert result == [{"trade_id": 1}]
        mongodb.query_range.assert_called_once_with(
            "trades_BTCUSDT", start, end, "BTCUSDT"
        )

    @pytest.mark.asyncio
    async def test_get_range_returns_empty_on_error(self):
        mongodb = Mock()
        mongodb.query_range = AsyncMock(side_effect=RuntimeError("x"))
        repo = TradeRepository(mysql_adapter=None, mongodb_adapter=mongodb)
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
        mongodb.query_latest = AsyncMock(return_value=[{"trade_id": 9}])
        repo = TradeRepository(mysql_adapter=None, mongodb_adapter=mongodb)
        assert await repo.get_latest("BTCUSDT", limit=3) == [{"trade_id": 9}]
        mongodb.query_latest.assert_called_once_with("trades_BTCUSDT", "BTCUSDT", 3)

    @pytest.mark.asyncio
    async def test_get_latest_returns_empty_on_error(self):
        mongodb = Mock()
        mongodb.query_latest = AsyncMock(side_effect=RuntimeError("x"))
        repo = TradeRepository(mysql_adapter=None, mongodb_adapter=mongodb)
        assert await repo.get_latest("BTCUSDT") == []

    @pytest.mark.asyncio
    async def test_count_passes_through(self):
        mongodb = Mock()
        mongodb.get_record_count = AsyncMock(return_value=42)
        repo = TradeRepository(mysql_adapter=None, mongodb_adapter=mongodb)
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = datetime(2026, 1, 2, tzinfo=UTC)
        assert await repo.count("BTCUSDT", start, end) == 42
        mongodb.get_record_count.assert_called_once_with(
            "trades_BTCUSDT", start, end, "BTCUSDT"
        )

    @pytest.mark.asyncio
    async def test_count_returns_zero_on_error(self):
        mongodb = Mock()
        mongodb.get_record_count = AsyncMock(side_effect=RuntimeError("boom"))
        repo = TradeRepository(mysql_adapter=None, mongodb_adapter=mongodb)
        assert await repo.count("BTCUSDT") == 0


class TestHealthRepository:
    @pytest.mark.asyncio
    async def test_insert_writes_health_record(self):
        mysql = Mock()
        mysql.write = Mock()
        repo = HealthRepository(mysql_adapter=mysql, mongodb_adapter=None)
        ok = await repo.insert("ds-1", "BTCUSDT", make_health_metrics())
        assert ok is True
        mysql.write.assert_called_once()
        # Second positional arg is the collection/table name.
        assert mysql.write.call_args[0][1] == "health_metrics"
        # First positional arg is the list of model-like wrappers.
        records = mysql.write.call_args[0][0]
        assert len(records) == 1
        dump = records[0].model_dump()
        assert dump["dataset_id"] == "ds-1"
        assert dump["symbol"] == "BTCUSDT"
        assert dump["completeness"] == 99.5
        assert dump["quality_score"] == 99.0
        assert "metric_id" in dump

    @pytest.mark.asyncio
    async def test_insert_returns_false_on_exception(self):
        mysql = Mock()
        mysql.write = Mock(side_effect=RuntimeError("write failed"))
        repo = HealthRepository(mysql_adapter=mysql, mongodb_adapter=None)
        assert await repo.insert("ds-1", "BTCUSDT", make_health_metrics()) is False

    def test_get_latest_health_returns_first_result(self):
        mysql = Mock()
        mysql.query_latest = Mock(return_value=[{"quality_score": 99.0}, {"x": 1}])
        repo = HealthRepository(mysql_adapter=mysql, mongodb_adapter=None)
        result = repo.get_latest_health("ds-1", "BTCUSDT")
        assert result == {"quality_score": 99.0}
        mysql.query_latest.assert_called_once_with(
            "health_metrics", symbol="BTCUSDT", limit=1
        )

    def test_get_latest_health_returns_none_when_empty(self):
        mysql = Mock()
        mysql.query_latest = Mock(return_value=[])
        repo = HealthRepository(mysql_adapter=mysql, mongodb_adapter=None)
        assert repo.get_latest_health("ds-1", "BTCUSDT") is None

    def test_get_latest_health_returns_none_on_exception(self):
        mysql = Mock()
        mysql.query_latest = Mock(side_effect=RuntimeError("read failed"))
        repo = HealthRepository(mysql_adapter=mysql, mongodb_adapter=None)
        assert repo.get_latest_health("ds-1", "BTCUSDT") is None
