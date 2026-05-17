"""
Coverage for data_manager.db.repositories.candle_repository.CandleRepository.

Covers both MongoDB and MySQL paths via the CANDLE_DATABASE_TYPE switch, plus
the mapping helpers (_get_collection_name, _get_mysql_table_name) and error
swallowing on each public method.
"""

from datetime import UTC, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, Mock, patch

import pytest

from data_manager.db.repositories.candle_repository import CandleRepository
from data_manager.models.market_data import Candle


def make_candle(symbol: str = "BTCUSDT", timeframe: str = "1h") -> Candle:
    return Candle(
        symbol=symbol,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("90"),
        close=Decimal("105"),
        volume=Decimal("1000"),
        timeframe=timeframe,
    )


class TestCollectionNaming:
    def test_collection_name_format(self):
        repo = CandleRepository(mysql_adapter=None, mongodb_adapter=None)
        assert repo._get_collection_name("BTCUSDT", "1h") == "candles_BTCUSDT_1h"
        assert repo._get_collection_name("ETHUSDT", "15m") == "candles_ETHUSDT_15m"


class TestMysqlTableNaming:
    @pytest.mark.parametrize(
        "tf,expected",
        [
            ("1h", "klines_h1"),
            ("4h", "klines_h4"),
            ("15m", "klines_m15"),
            ("1m", "klines_m1"),
            ("1d", "klines_d1"),
            ("1w", "klines_w1"),
        ],
    )
    def test_known_timeframes(self, tf, expected):
        repo = CandleRepository(mysql_adapter=None, mongodb_adapter=None)
        assert repo._get_mysql_table_name(tf) == expected

    def test_empty_timeframe_returns_unknown(self):
        repo = CandleRepository(mysql_adapter=None, mongodb_adapter=None)
        assert repo._get_mysql_table_name("") == "klines_unknown"


class TestMongoPath:
    @pytest.mark.asyncio
    async def test_insert_writes_to_candles_collection(self):
        with patch(
            "data_manager.db.repositories.candle_repository.constants.CANDLE_DATABASE_TYPE",
            "mongodb",
        ):
            mongodb = Mock()
            mongodb.write = AsyncMock(return_value=1)
            repo = CandleRepository(mysql_adapter=None, mongodb_adapter=mongodb)
            assert await repo.insert(make_candle("BTCUSDT", "1h")) is True
            assert mongodb.write.call_args[0][1] == "candles_BTCUSDT_1h"

    @pytest.mark.asyncio
    async def test_insert_returns_false_when_no_records_written(self):
        with patch(
            "data_manager.db.repositories.candle_repository.constants.CANDLE_DATABASE_TYPE",
            "mongodb",
        ):
            mongodb = Mock()
            mongodb.write = AsyncMock(return_value=0)
            repo = CandleRepository(mysql_adapter=None, mongodb_adapter=mongodb)
            assert await repo.insert(make_candle()) is False

    @pytest.mark.asyncio
    async def test_insert_returns_false_on_exception(self):
        with patch(
            "data_manager.db.repositories.candle_repository.constants.CANDLE_DATABASE_TYPE",
            "mongodb",
        ):
            mongodb = Mock()
            mongodb.write = AsyncMock(side_effect=RuntimeError("boom"))
            repo = CandleRepository(mysql_adapter=None, mongodb_adapter=mongodb)
            assert await repo.insert(make_candle()) is False

    @pytest.mark.asyncio
    async def test_batch_empty_returns_zero(self):
        with patch(
            "data_manager.db.repositories.candle_repository.constants.CANDLE_DATABASE_TYPE",
            "mongodb",
        ):
            repo = CandleRepository(mysql_adapter=None, mongodb_adapter=Mock())
            assert await repo.insert_batch([]) == 0

    @pytest.mark.asyncio
    async def test_batch_groups_by_symbol_and_timeframe(self):
        with patch(
            "data_manager.db.repositories.candle_repository.constants.CANDLE_DATABASE_TYPE",
            "mongodb",
        ):
            mongodb = Mock()
            mongodb.write = AsyncMock(side_effect=[2, 1, 1])
            repo = CandleRepository(mysql_adapter=None, mongodb_adapter=mongodb)
            total = await repo.insert_batch(
                [
                    make_candle("BTCUSDT", "1h"),
                    make_candle("BTCUSDT", "1h"),
                    make_candle("BTCUSDT", "15m"),
                    make_candle("ETHUSDT", "1h"),
                ]
            )
            assert total == 4
            # Three distinct collections: BTCUSDT-1h, BTCUSDT-15m, ETHUSDT-1h
            assert mongodb.write.call_count == 3

    @pytest.mark.asyncio
    async def test_batch_returns_zero_on_exception(self):
        with patch(
            "data_manager.db.repositories.candle_repository.constants.CANDLE_DATABASE_TYPE",
            "mongodb",
        ):
            mongodb = Mock()
            mongodb.write = AsyncMock(side_effect=RuntimeError("boom"))
            repo = CandleRepository(mysql_adapter=None, mongodb_adapter=mongodb)
            assert await repo.insert_batch([make_candle()]) == 0

    @pytest.mark.asyncio
    async def test_get_range_passes_through_to_mongodb(self):
        with patch(
            "data_manager.db.repositories.candle_repository.constants.CANDLE_DATABASE_TYPE",
            "mongodb",
        ):
            mongodb = Mock()
            mongodb.query_range = AsyncMock(return_value=[{"close": "100"}])
            repo = CandleRepository(mysql_adapter=None, mongodb_adapter=mongodb)
            start = datetime(2026, 1, 1, tzinfo=UTC)
            end = datetime(2026, 1, 2, tzinfo=UTC)
            assert await repo.get_range("BTCUSDT", "1h", start, end) == [
                {"close": "100"}
            ]
            mongodb.query_range.assert_called_once_with(
                "candles_BTCUSDT_1h", start, end, "BTCUSDT"
            )

    @pytest.mark.asyncio
    async def test_get_range_returns_empty_on_exception(self):
        with patch(
            "data_manager.db.repositories.candle_repository.constants.CANDLE_DATABASE_TYPE",
            "mongodb",
        ):
            mongodb = Mock()
            mongodb.query_range = AsyncMock(side_effect=RuntimeError("x"))
            repo = CandleRepository(mysql_adapter=None, mongodb_adapter=mongodb)
            assert (
                await repo.get_range(
                    "BTCUSDT",
                    "1h",
                    datetime(2026, 1, 1, tzinfo=UTC),
                    datetime(2026, 1, 2, tzinfo=UTC),
                )
                == []
            )

    @pytest.mark.asyncio
    async def test_get_latest_passes_through(self):
        with patch(
            "data_manager.db.repositories.candle_repository.constants.CANDLE_DATABASE_TYPE",
            "mongodb",
        ):
            mongodb = Mock()
            mongodb.query_latest = AsyncMock(return_value=[{"close": "1"}])
            repo = CandleRepository(mysql_adapter=None, mongodb_adapter=mongodb)
            assert await repo.get_latest("BTCUSDT", "1h", limit=5) == [{"close": "1"}]
            mongodb.query_latest.assert_called_once_with(
                "candles_BTCUSDT_1h", "BTCUSDT", 5
            )

    @pytest.mark.asyncio
    async def test_get_latest_returns_empty_on_exception(self):
        with patch(
            "data_manager.db.repositories.candle_repository.constants.CANDLE_DATABASE_TYPE",
            "mongodb",
        ):
            mongodb = Mock()
            mongodb.query_latest = AsyncMock(side_effect=RuntimeError("x"))
            repo = CandleRepository(mysql_adapter=None, mongodb_adapter=mongodb)
            assert await repo.get_latest("BTCUSDT", "1h") == []

    @pytest.mark.asyncio
    async def test_count_passes_through(self):
        with patch(
            "data_manager.db.repositories.candle_repository.constants.CANDLE_DATABASE_TYPE",
            "mongodb",
        ):
            mongodb = Mock()
            mongodb.get_record_count = AsyncMock(return_value=100)
            repo = CandleRepository(mysql_adapter=None, mongodb_adapter=mongodb)
            assert await repo.count("BTCUSDT", "1h") == 100
            mongodb.get_record_count.assert_called_once_with(
                "candles_BTCUSDT_1h", None, None, "BTCUSDT"
            )

    @pytest.mark.asyncio
    async def test_count_returns_zero_on_exception(self):
        with patch(
            "data_manager.db.repositories.candle_repository.constants.CANDLE_DATABASE_TYPE",
            "mongodb",
        ):
            mongodb = Mock()
            mongodb.get_record_count = AsyncMock(side_effect=RuntimeError("x"))
            repo = CandleRepository(mysql_adapter=None, mongodb_adapter=mongodb)
            assert await repo.count("BTCUSDT", "1h") == 0

    @pytest.mark.asyncio
    async def test_ensure_indexes_calls_mongodb(self):
        with patch(
            "data_manager.db.repositories.candle_repository.constants.CANDLE_DATABASE_TYPE",
            "mongodb",
        ):
            mongodb = Mock()
            mongodb.ensure_indexes = AsyncMock()
            repo = CandleRepository(mysql_adapter=None, mongodb_adapter=mongodb)
            await repo.ensure_indexes("BTCUSDT", "1h")
            mongodb.ensure_indexes.assert_called_once_with("candles_BTCUSDT_1h")

    @pytest.mark.asyncio
    async def test_ensure_indexes_swallows_exception(self):
        with patch(
            "data_manager.db.repositories.candle_repository.constants.CANDLE_DATABASE_TYPE",
            "mongodb",
        ):
            mongodb = Mock()
            mongodb.ensure_indexes = AsyncMock(side_effect=RuntimeError("x"))
            repo = CandleRepository(mysql_adapter=None, mongodb_adapter=mongodb)
            # Must not raise.
            await repo.ensure_indexes("BTCUSDT", "1h")
            mongodb.ensure_indexes.assert_called_once()


class TestMysqlPath:
    @pytest.mark.asyncio
    async def test_insert_writes_to_mysql_table(self):
        with patch(
            "data_manager.db.repositories.candle_repository.constants.CANDLE_DATABASE_TYPE",
            "mysql",
        ):
            mysql = Mock()
            mysql.write = Mock(return_value=1)
            repo = CandleRepository(mysql_adapter=mysql, mongodb_adapter=None)
            assert await repo.insert(make_candle("BTCUSDT", "1h")) is True
            assert mysql.write.call_args[0][1] == "klines_h1"

    @pytest.mark.asyncio
    async def test_batch_groups_by_table(self):
        with patch(
            "data_manager.db.repositories.candle_repository.constants.CANDLE_DATABASE_TYPE",
            "mysql",
        ):
            mysql = Mock()
            mysql.write_batch = Mock(side_effect=[2, 1])
            repo = CandleRepository(mysql_adapter=mysql, mongodb_adapter=None)
            total = await repo.insert_batch(
                [
                    make_candle("BTCUSDT", "1h"),
                    make_candle("ETHUSDT", "1h"),
                    make_candle("BTCUSDT", "15m"),
                ]
            )
            assert total == 3
            assert mysql.write_batch.call_count == 2

    @pytest.mark.asyncio
    async def test_get_range_maps_mysql_columns(self):
        with patch(
            "data_manager.db.repositories.candle_repository.constants.CANDLE_DATABASE_TYPE",
            "mysql",
        ):
            mysql = Mock()
            mysql.query_range = Mock(
                return_value=[
                    {
                        "open_price": "100",
                        "high_price": "110",
                        "low_price": "90",
                        "close_price": "105",
                        "volume": "1000",
                        "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
                        "symbol": "BTCUSDT",
                        "interval": "1h",
                    }
                ]
            )
            repo = CandleRepository(mysql_adapter=mysql, mongodb_adapter=None)
            result = await repo.get_range(
                "BTCUSDT",
                "1h",
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2026, 1, 2, tzinfo=UTC),
            )
            assert len(result) == 1
            # MySQL columns get renamed to standard candle keys.
            assert result[0]["open"] == "100"
            assert result[0]["high"] == "110"
            assert result[0]["close"] == "105"

    @pytest.mark.asyncio
    async def test_get_latest_maps_mysql_columns(self):
        with patch(
            "data_manager.db.repositories.candle_repository.constants.CANDLE_DATABASE_TYPE",
            "mysql",
        ):
            mysql = Mock()
            mysql.query_latest = Mock(
                return_value=[
                    {
                        "open_price": "100",
                        "high_price": "110",
                        "low_price": "90",
                        "close_price": "105",
                        "volume": "1000",
                        "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
                        "symbol": "BTCUSDT",
                        "interval": "1h",
                    }
                ]
            )
            repo = CandleRepository(mysql_adapter=mysql, mongodb_adapter=None)
            result = await repo.get_latest("BTCUSDT", "1h", limit=1)
            assert result[0]["open"] == "100"

    @pytest.mark.asyncio
    async def test_count_passes_through(self):
        with patch(
            "data_manager.db.repositories.candle_repository.constants.CANDLE_DATABASE_TYPE",
            "mysql",
        ):
            mysql = Mock()
            mysql.get_record_count = Mock(return_value=42)
            repo = CandleRepository(mysql_adapter=mysql, mongodb_adapter=None)
            assert await repo.count("BTCUSDT", "1h") == 42
            mysql.get_record_count.assert_called_once_with(
                "klines_h1", None, None, "BTCUSDT"
            )

    @pytest.mark.asyncio
    async def test_ensure_indexes_is_noop_on_mysql(self):
        with patch(
            "data_manager.db.repositories.candle_repository.constants.CANDLE_DATABASE_TYPE",
            "mysql",
        ):
            # MySQL indexes are handled during table creation; ensure_indexes must
            # not call any mongodb path.
            mysql = Mock()
            repo = CandleRepository(mysql_adapter=mysql, mongodb_adapter=None)
            await repo.ensure_indexes("BTCUSDT", "1h")
            # No assert other than reaching this line — but assert that no
            # mongodb-side call was made by virtue of having no adapter.
            assert repo.mongodb is None
