"""
Coverage for the candle-store cutover safety net (petrosa-data-manager#275).

- AC1 warm-up backfill: ``data_manager.maintenance.candle_warmup_backfill``
- AC2 readiness gate: ``data_manager.maintenance.candle_readiness``
- AC3 no-empty-read window: ``CandleRepository`` fallback reads +
  lookback-aware default window in ``data_manager.api.routes.data``
- AC4 rollback: dual-write mirror
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, Mock, patch

import pytest

from data_manager.api.routes.data import _completeness_pct, _default_candle_start
from data_manager.db.repositories.candle_repository import (
    MYSQL_CANDLE_COLUMNS,
    CandleRepository,
    map_mysql_row,
    mongo_collection_name,
    mysql_table_name,
)
from data_manager.maintenance import candle_warmup_backfill as warmup
from data_manager.maintenance.candle_readiness import (
    ReadinessReport,
    evaluate_collection,
    evaluate_readiness,
)
from data_manager.models.market_data import Candle

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def klines_row(ts: datetime, symbol: str = "BTCUSDT") -> dict:
    return {
        "symbol": symbol,
        "timestamp": ts,
        "open_price": Decimal("100"),
        "high_price": Decimal("110"),
        "low_price": Decimal("90"),
        "close_price": Decimal("105"),
        "volume": Decimal("1000"),
        "quote_asset_volume": Decimal("105000"),
        "number_of_trades": 42,
        "interval": "1h",
    }


def mongo_adapter(*, count: int = 0, latest: list | None = None) -> Mock:
    adapter = Mock()
    adapter.get_record_count = AsyncMock(return_value=count)
    adapter.query_latest = AsyncMock(return_value=latest if latest is not None else [])
    adapter.query_range = AsyncMock(return_value=[])
    adapter.write = AsyncMock(return_value=0)
    adapter.ensure_indexes = AsyncMock()
    adapter.delete_range = AsyncMock(return_value=0)
    return adapter


# ---------------------------------------------------------------------------
# AC2 — readiness gate
# ---------------------------------------------------------------------------


class TestReadinessGate:
    @pytest.mark.asyncio
    async def test_deep_and_fresh_collection_is_ready(self):
        adapter = mongo_adapter(
            count=400, latest=[{"timestamp": NOW - timedelta(minutes=30)}]
        )
        verdict = await evaluate_collection(
            adapter,
            "BTCUSDT",
            "1h",
            required_count=400,
            freshness_intervals=3,
            now=NOW,
        )
        assert verdict.ready is True
        assert verdict.reasons == []
        assert verdict.collection == "candles_BTCUSDT_1h"

    @pytest.mark.asyncio
    async def test_shallow_collection_is_not_ready(self):
        adapter = mongo_adapter(count=399, latest=[{"timestamp": NOW}])
        verdict = await evaluate_collection(
            adapter,
            "BTCUSDT",
            "1h",
            required_count=400,
            freshness_intervals=3,
            now=NOW,
        )
        assert verdict.ready is False
        assert "depth 399 < required 400" in verdict.reasons

    @pytest.mark.asyncio
    async def test_deep_but_stale_collection_is_not_ready(self):
        # Backfilled once, never kept current: 400 candles but 10h old on a 1h
        # timeframe (budget = 3 intervals).
        adapter = mongo_adapter(
            count=400, latest=[{"timestamp": NOW - timedelta(hours=10)}]
        )
        verdict = await evaluate_collection(
            adapter,
            "BTCUSDT",
            "1h",
            required_count=400,
            freshness_intervals=3,
            now=NOW,
        )
        assert verdict.ready is False
        assert any("old" in reason for reason in verdict.reasons)

    @pytest.mark.asyncio
    async def test_empty_collection_is_not_ready(self):
        adapter = mongo_adapter(count=0, latest=[])
        verdict = await evaluate_collection(
            adapter, "BTCUSDT", "1h", required_count=400, freshness_intervals=3, now=NOW
        )
        assert verdict.ready is False
        assert "no candles present" in verdict.reasons

    @pytest.mark.asyncio
    async def test_backend_error_fails_closed(self):
        adapter = mongo_adapter()
        adapter.get_record_count = AsyncMock(side_effect=RuntimeError("atlas down"))
        verdict = await evaluate_collection(
            adapter, "BTCUSDT", "1h", required_count=400, freshness_intervals=3, now=NOW
        )
        assert verdict.ready is False
        assert any("query failed" in reason for reason in verdict.reasons)

    @pytest.mark.asyncio
    async def test_unparseable_timeframe_fails_closed(self):
        adapter = mongo_adapter(count=1000, latest=[{"timestamp": NOW}])
        verdict = await evaluate_collection(
            adapter,
            "BTCUSDT",
            "banana",
            required_count=400,
            freshness_intervals=3,
            now=NOW,
        )
        assert verdict.ready is False
        assert adapter.get_record_count.await_count == 0

    @pytest.mark.asyncio
    async def test_non_datetime_timestamp_fails_closed(self):
        adapter = mongo_adapter(
            count=400, latest=[{"timestamp": "2026-09-11T12:00:00"}]
        )
        verdict = await evaluate_collection(
            adapter, "BTCUSDT", "1h", required_count=400, freshness_intervals=3, now=NOW
        )
        assert verdict.ready is False

    @pytest.mark.asyncio
    async def test_grid_report_is_ready_only_when_every_collection_is(self):
        adapter = mongo_adapter(
            count=400, latest=[{"timestamp": NOW - timedelta(minutes=1)}]
        )
        report = await evaluate_readiness(
            adapter,
            pairs=["BTCUSDT", "ETHUSDT"],
            timeframes=["5m", "1h"],
            required_count=400,
            freshness_intervals=3,
            now=NOW,
        )
        assert report.ready is True
        assert len(report.collections) == 4
        assert report.not_ready == []

    @pytest.mark.asyncio
    async def test_one_cold_collection_blocks_the_whole_grid(self):
        adapter = mongo_adapter()
        adapter.get_record_count = AsyncMock(side_effect=[400, 10])
        adapter.query_latest = AsyncMock(return_value=[{"timestamp": NOW}])
        report = await evaluate_readiness(
            adapter,
            pairs=["BTCUSDT"],
            timeframes=["5m", "1h"],
            required_count=400,
            freshness_intervals=3,
            now=NOW,
        )
        assert report.ready is False
        assert len(report.not_ready) == 1

    @pytest.mark.asyncio
    async def test_empty_grid_is_not_ready(self):
        # "Nothing checked" must never read as "everything verified".
        adapter = mongo_adapter()
        report = await evaluate_readiness(
            adapter, pairs=[], timeframes=["1h"], required_count=400, now=NOW
        )
        assert report.ready is False
        assert report.collections == []

    def test_report_to_dict_is_json_shaped(self):
        report = ReadinessReport(
            ready=False,
            checked_at=NOW.isoformat(),
            required_count=400,
            freshness_intervals=3.0,
            collections=[],
        )
        payload = report.to_dict()
        assert payload["ready"] is False
        assert payload["required_count"] == 400
        assert payload["collections"] == []


# ---------------------------------------------------------------------------
# AC1 — warm-up backfill
# ---------------------------------------------------------------------------


class TestRowMapping:
    def test_maps_klines_columns_onto_candle(self):
        candle = warmup.row_to_candle(klines_row(NOW), "1h")
        assert candle is not None
        assert candle.symbol == "BTCUSDT"
        assert candle.close == Decimal("105")
        assert candle.quote_volume == Decimal("105000")
        assert candle.trades_count == 42
        assert candle.timeframe == "1h"

    def test_naive_mysql_timestamp_becomes_utc_aware(self):
        row = klines_row(datetime(2026, 9, 11, 12, 0))
        candle = warmup.row_to_candle(row, "1h")
        assert candle is not None
        assert candle.timestamp.tzinfo is not None

    def test_unmappable_row_returns_none_instead_of_raising(self):
        assert warmup.row_to_candle({"symbol": "BTCUSDT"}, "1h") is None
        broken = klines_row(NOW)
        del broken["close_price"]
        assert warmup.row_to_candle(broken, "1h") is None

    def test_mysql_table_name_mapping(self):
        assert mysql_table_name("1h") == "klines_h1"
        assert mysql_table_name("15m") == "klines_m15"
        assert mysql_table_name("") == "klines_unknown"

    def test_mongo_collection_name_mapping(self):
        assert mongo_collection_name("BTCUSDT", "5m") == "candles_BTCUSDT_5m"


class TestWarmupBackfill:
    def _config(self, **overrides) -> warmup.BackfillConfig:
        config = warmup.BackfillConfig(
            pairs=["BTCUSDT"],
            timeframes=["1h"],
            min_candles=3,
            batch_size=2,
            freshness_intervals=3,
            trim_enabled=False,
        )
        for key, value in overrides.items():
            setattr(config, key, value)
        return config

    @pytest.mark.asyncio
    async def test_writes_source_rows_into_mongo_in_batches(self):
        rows = [klines_row(NOW - timedelta(hours=i)) for i in range(3)]
        mysql = Mock()
        mysql.query_latest = Mock(return_value=rows)
        mongo = mongo_adapter(count=0, latest=[])
        mongo.write = AsyncMock(side_effect=[2, 1])

        result = await warmup.backfill_pair(
            mysql, mongo, "BTCUSDT", "1h", self._config(), now=NOW
        )

        assert result.error is None
        assert result.source_rows == 3
        assert result.written == 3
        assert mongo.write.await_count == 2  # batch_size=2 over 3 candles
        mysql.query_latest.assert_called_once_with(
            "klines_h1",
            "BTCUSDT",
            3,
            columns=MYSQL_CANDLE_COLUMNS
            + ("open_time", "quote_asset_volume", "number_of_trades"),
        )
        mongo.ensure_indexes.assert_awaited_once_with("candles_BTCUSDT_1h")

    @pytest.mark.asyncio
    async def test_already_warm_collection_is_skipped(self):
        mysql = Mock()
        mysql.query_latest = Mock(return_value=[klines_row(NOW)])
        mongo = mongo_adapter(count=3, latest=[{"timestamp": NOW}])

        result = await warmup.backfill_pair(
            mysql, mongo, "BTCUSDT", "1h", self._config(), now=NOW
        )

        assert result.skipped is True
        assert result.written == 0
        mysql.query_latest.assert_not_called()

    @pytest.mark.asyncio
    async def test_force_backfills_even_a_warm_collection(self):
        mysql = Mock()
        mysql.query_latest = Mock(return_value=[klines_row(NOW)])
        mongo = mongo_adapter(count=3, latest=[{"timestamp": NOW}])
        mongo.write = AsyncMock(return_value=1)

        result = await warmup.backfill_pair(
            mysql, mongo, "BTCUSDT", "1h", self._config(force=True), now=NOW
        )

        assert result.skipped is False
        assert result.written == 1

    @pytest.mark.asyncio
    async def test_dry_run_writes_nothing(self):
        mysql = Mock()
        mysql.query_latest = Mock(return_value=[klines_row(NOW)])
        mongo = mongo_adapter(count=0, latest=[])

        result = await warmup.backfill_pair(
            mysql, mongo, "BTCUSDT", "1h", self._config(dry_run=True), now=NOW
        )

        assert result.written == 1
        mongo.write.assert_not_awaited()
        mongo.ensure_indexes.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rerun_is_idempotent_when_source_is_unchanged(self):
        # MongoDBAdapter.write returns 0 for duplicates (deterministic _id), so
        # a second pass over the same source rows adds nothing.
        rows = [klines_row(NOW - timedelta(hours=i)) for i in range(3)]
        mysql = Mock()
        mysql.query_latest = Mock(return_value=rows)
        mongo = mongo_adapter(count=0, latest=[])
        mongo.write = AsyncMock(return_value=0)

        result = await warmup.backfill_pair(
            mysql, mongo, "BTCUSDT", "1h", self._config(force=True), now=NOW
        )

        assert result.written == 0
        assert result.error is None

    @pytest.mark.asyncio
    async def test_missing_source_rows_is_reported_not_raised(self):
        mysql = Mock()
        mysql.query_latest = Mock(return_value=[])
        mongo = mongo_adapter(count=0, latest=[])

        result = await warmup.backfill_pair(
            mysql, mongo, "BTCUSDT", "1h", self._config(), now=NOW
        )

        assert result.written == 0
        assert result.skip_reason is not None
        assert "no source rows" in result.skip_reason

    @pytest.mark.asyncio
    async def test_backend_error_is_captured_on_the_result(self):
        mysql = Mock()
        mysql.query_latest = Mock(side_effect=RuntimeError("mysql gone"))
        mongo = mongo_adapter(count=0, latest=[])

        result = await warmup.backfill_pair(
            mysql, mongo, "BTCUSDT", "1h", self._config(), now=NOW
        )

        assert result.error == "mysql gone"

    @pytest.mark.asyncio
    async def test_trim_caps_the_collection_at_the_keep_window(self):
        rows = [klines_row(NOW)]
        mysql = Mock()
        mysql.query_latest = Mock(return_value=rows)
        cutoff = NOW - timedelta(hours=3)
        mongo = mongo_adapter(count=0, latest=[])
        mongo.write = AsyncMock(return_value=1)
        mongo.query_latest = AsyncMock(
            return_value=[{"timestamp": NOW}, {"timestamp": NOW}, {"timestamp": cutoff}]
        )
        mongo.delete_range = AsyncMock(return_value=7)

        config = self._config(trim_enabled=True, trim_factor=1.0)
        result = await warmup.backfill_pair(
            mysql, mongo, "BTCUSDT", "1h", config, now=NOW
        )

        assert result.trimmed == 7
        args = mongo.delete_range.await_args[0]
        assert args[0] == "candles_BTCUSDT_1h"
        assert args[2] == cutoff
        assert args[3] == "BTCUSDT"

    @pytest.mark.asyncio
    async def test_trim_is_a_noop_when_collection_is_below_the_cap(self):
        mysql = Mock()
        mysql.query_latest = Mock(return_value=[klines_row(NOW)])
        mongo = mongo_adapter(count=0, latest=[])
        mongo.write = AsyncMock(return_value=1)
        mongo.query_latest = AsyncMock(return_value=[{"timestamp": NOW}])

        config = self._config(trim_enabled=True, trim_factor=1.0)
        result = await warmup.backfill_pair(
            mysql, mongo, "BTCUSDT", "1h", config, now=NOW
        )

        assert result.trimmed == 0
        mongo.delete_range.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_run_backfill_walks_the_whole_grid(self):
        mysql = Mock()
        mysql.query_latest = Mock(return_value=[klines_row(NOW)])
        mongo = mongo_adapter(count=0, latest=[])
        mongo.write = AsyncMock(return_value=1)

        config = self._config(pairs=["BTCUSDT", "ETHUSDT"], timeframes=["5m", "1h"])
        results = await warmup.run_backfill(mysql, mongo, config, now=NOW)

        assert len(results) == 4
        assert {r.collection for r in results} == {
            "candles_BTCUSDT_5m",
            "candles_BTCUSDT_1h",
            "candles_ETHUSDT_5m",
            "candles_ETHUSDT_1h",
        }

    def test_config_from_env_defaults_to_the_276_contract(self):
        config = warmup.load_config_from_env({})
        assert config.min_candles == 400
        assert config.batch_size >= 1
        assert config.timeframes

    def test_config_from_env_ignores_garbage_overrides(self):
        config = warmup.load_config_from_env({"CANDLE_WARMUP_MIN_CANDLES": "lots"})
        assert config.min_candles == 400


# ---------------------------------------------------------------------------
# AC3 — no empty-read window
# ---------------------------------------------------------------------------


def _patch_primary(value: str):
    return patch(
        "data_manager.db.repositories.candle_repository.constants.CANDLE_DATABASE_TYPE",
        value,
    )


def _patch_fallback(enabled: bool):
    return patch(
        "data_manager.db.repositories.candle_repository.constants."
        "CANDLE_READ_FALLBACK_ENABLED",
        enabled,
    )


def _patch_dual_write(enabled: bool):
    return patch(
        "data_manager.db.repositories.candle_repository.constants."
        "CANDLE_DUAL_WRITE_ENABLED",
        enabled,
    )


class TestReadFallback:
    @pytest.mark.asyncio
    async def test_empty_mongo_range_falls_back_to_mysql(self):
        with _patch_primary("mongodb"), _patch_fallback(True):
            mongodb = Mock()
            mongodb.query_range = AsyncMock(return_value=[])
            mysql = Mock()
            mysql.query_range = Mock(return_value=[klines_row(NOW)])
            repo = CandleRepository(mysql_adapter=mysql, mongodb_adapter=mongodb)

            result = await repo.get_range(
                "BTCUSDT", "1h", NOW - timedelta(hours=2), NOW
            )

            assert len(result) == 1
            assert result[0]["close"] == Decimal("105")
            assert repo.last_read_source == "mysql"
            mysql.query_range.assert_called_once()

    @pytest.mark.asyncio
    async def test_kill_switch_disables_the_fallback(self):
        with _patch_primary("mongodb"), _patch_fallback(False):
            mongodb = Mock()
            mongodb.query_range = AsyncMock(return_value=[])
            mysql = Mock()
            mysql.query_range = Mock(return_value=[klines_row(NOW)])
            repo = CandleRepository(mysql_adapter=mysql, mongodb_adapter=mongodb)

            assert await repo.get_range("BTCUSDT", "1h", NOW, NOW) == []
            assert repo.last_read_source == "mongodb"
            mysql.query_range.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_fallback_when_secondary_adapter_is_absent(self):
        with _patch_primary("mongodb"), _patch_fallback(True):
            mongodb = Mock()
            mongodb.query_range = AsyncMock(return_value=[])
            repo = CandleRepository(mysql_adapter=None, mongodb_adapter=mongodb)
            assert await repo.get_range("BTCUSDT", "1h", NOW, NOW) == []

    @pytest.mark.asyncio
    async def test_primary_exception_still_falls_back(self):
        with _patch_primary("mongodb"), _patch_fallback(True):
            mongodb = Mock()
            mongodb.query_range = AsyncMock(side_effect=RuntimeError("atlas down"))
            mysql = Mock()
            mysql.query_range = Mock(return_value=[klines_row(NOW)])
            repo = CandleRepository(mysql_adapter=mysql, mongodb_adapter=mongodb)

            result = await repo.get_range("BTCUSDT", "1h", NOW, NOW)

            assert len(result) == 1
            assert repo.last_read_source == "mysql"

    @pytest.mark.asyncio
    async def test_short_latest_window_falls_back(self):
        # A partially warm Mongo collection is as dangerous as an empty one.
        with _patch_primary("mongodb"), _patch_fallback(True):
            mongodb = Mock()
            mongodb.query_latest = AsyncMock(return_value=[{"close": "1"}])
            mysql = Mock()
            mysql.query_latest = Mock(return_value=[klines_row(NOW)] * 5)
            repo = CandleRepository(mysql_adapter=mysql, mongodb_adapter=mongodb)

            result = await repo.get_latest("BTCUSDT", "1h", limit=5)

            assert len(result) == 5
            assert repo.last_read_source == "mysql"

    @pytest.mark.asyncio
    async def test_full_primary_window_does_not_fall_back(self):
        with _patch_primary("mongodb"), _patch_fallback(True):
            mongodb = Mock()
            mongodb.query_latest = AsyncMock(return_value=[{"close": "1"}] * 5)
            mysql = Mock()
            mysql.query_latest = Mock(return_value=[klines_row(NOW)] * 5)
            repo = CandleRepository(mysql_adapter=mysql, mongodb_adapter=mongodb)

            result = await repo.get_latest("BTCUSDT", "1h", limit=5)

            assert len(result) == 5
            assert repo.last_read_source == "mongodb"
            mysql.query_latest.assert_not_called()

    @pytest.mark.asyncio
    async def test_shorter_fallback_does_not_replace_primary_window(self):
        with _patch_primary("mongodb"), _patch_fallback(True):
            mongodb = Mock()
            mongodb.query_latest = AsyncMock(return_value=[{"close": "1"}] * 3)
            mysql = Mock()
            mysql.query_latest = Mock(return_value=[klines_row(NOW)])
            repo = CandleRepository(mysql_adapter=mysql, mongodb_adapter=mongodb)

            result = await repo.get_latest("BTCUSDT", "1h", limit=5)

            assert len(result) == 3
            assert repo.last_read_source == "mongodb"

    @pytest.mark.asyncio
    async def test_rollback_direction_falls_back_to_mongo(self):
        # After a rollback MySQL is primary again; a hole in MySQL must be
        # covered by Mongo, not surfaced as an empty window (AC4).
        with _patch_primary("mysql"), _patch_fallback(True):
            mysql = Mock()
            mysql.query_range = Mock(return_value=[])
            mongodb = Mock()
            mongodb.query_range = AsyncMock(return_value=[{"close": "105"}])
            repo = CandleRepository(mysql_adapter=mysql, mongodb_adapter=mongodb)

            result = await repo.get_range("BTCUSDT", "1h", NOW, NOW)

            assert result == [{"close": "105"}]
            assert repo.last_read_source == "mongodb"

    @pytest.mark.asyncio
    async def test_fallback_failure_is_swallowed(self):
        with _patch_primary("mongodb"), _patch_fallback(True):
            mongodb = Mock()
            mongodb.query_range = AsyncMock(return_value=[])
            mysql = Mock()
            mysql.query_range = Mock(side_effect=RuntimeError("mysql gone too"))
            repo = CandleRepository(mysql_adapter=mysql, mongodb_adapter=mongodb)

            assert await repo.get_range("BTCUSDT", "1h", NOW, NOW) == []


class TestDualWrite:
    @pytest.mark.asyncio
    async def test_disabled_by_default_writes_only_to_primary(self):
        with _patch_primary("mongodb"), _patch_dual_write(False):
            mongodb = Mock()
            mongodb.write = AsyncMock(return_value=1)
            mysql = Mock()
            repo = CandleRepository(mysql_adapter=mysql, mongodb_adapter=mongodb)

            assert await repo.insert(_candle()) is True
            mysql.write_batch.assert_not_called()

    @pytest.mark.asyncio
    async def test_enabled_mirrors_mongo_writes_into_mysql(self):
        with _patch_primary("mongodb"), _patch_dual_write(True):
            mongodb = Mock()
            mongodb.write = AsyncMock(return_value=1)
            mysql = Mock()
            mysql.write_batch = Mock(return_value=1)
            repo = CandleRepository(mysql_adapter=mysql, mongodb_adapter=mongodb)

            assert await repo.insert(_candle()) is True
            mysql.write_batch.assert_called_once()
            assert mysql.write_batch.call_args[0][1] == "klines_h1"

    @pytest.mark.asyncio
    async def test_enabled_mirrors_mysql_writes_into_mongo(self):
        with _patch_primary("mysql"), _patch_dual_write(True):
            mysql = Mock()
            mysql.write_batch = Mock(return_value=2)
            mongodb = Mock()
            mongodb.write = AsyncMock(return_value=2)
            repo = CandleRepository(mysql_adapter=mysql, mongodb_adapter=mongodb)

            total = await repo.insert_batch([_candle(), _candle("ETHUSDT")])

            assert total == 2
            assert mongodb.write.await_count == 2  # one per symbol collection

    @pytest.mark.asyncio
    async def test_mirror_failure_does_not_fail_the_primary_write(self):
        with _patch_primary("mongodb"), _patch_dual_write(True):
            mongodb = Mock()
            mongodb.write = AsyncMock(return_value=1)
            mysql = Mock()
            mysql.write_batch = Mock(side_effect=RuntimeError("mysql down"))
            repo = CandleRepository(mysql_adapter=mysql, mongodb_adapter=mongodb)

            assert await repo.insert(_candle()) is True

    @pytest.mark.asyncio
    async def test_mirror_skipped_when_secondary_adapter_absent(self):
        with _patch_primary("mongodb"), _patch_dual_write(True):
            mongodb = Mock()
            mongodb.write = AsyncMock(return_value=1)
            repo = CandleRepository(mysql_adapter=None, mongodb_adapter=mongodb)
            assert await repo.insert(_candle()) is True


def _candle(symbol: str = "BTCUSDT") -> Candle:
    return Candle(
        symbol=symbol,
        timestamp=NOW,
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("90"),
        close=Decimal("105"),
        volume=Decimal("1000"),
        timeframe="1h",
    )


class TestMysqlRowMapping:
    def test_map_mysql_row_renames_columns(self):
        mapped = map_mysql_row(klines_row(NOW))
        assert mapped["open"] == Decimal("100")
        assert mapped["close"] == Decimal("105")
        assert mapped["symbol"] == "BTCUSDT"

    def test_map_mysql_row_tolerates_missing_columns(self):
        assert map_mysql_row({}) == {
            "open": None,
            "high": None,
            "low": None,
            "close": None,
            "volume": None,
            "timestamp": None,
            "symbol": None,
            "interval": None,
        }


# ---------------------------------------------------------------------------
# AC3/AC5 — API default lookback window + completeness signal
# ---------------------------------------------------------------------------


class TestDefaultCandleWindow:
    def test_window_scales_with_the_requested_timeframe(self):
        # 250 x 1h must not be squeezed into the old hardcoded 24h lookback.
        start = _default_candle_start(NOW, "1h", limit=250, offset=0)
        assert (NOW - start) >= timedelta(hours=250)

    def test_window_scales_for_daily_candles(self):
        start = _default_candle_start(NOW, "1d", limit=400, offset=0)
        assert (NOW - start) >= timedelta(days=400)

    def test_window_accounts_for_pagination_offset(self):
        no_offset = _default_candle_start(NOW, "5m", limit=100, offset=0)
        with_offset = _default_candle_start(NOW, "5m", limit=100, offset=200)
        assert with_offset < no_offset

    def test_unparseable_period_falls_back_to_24h(self):
        start = _default_candle_start(NOW, "banana", limit=100, offset=0)
        assert NOW - start == timedelta(hours=24)


class TestCompleteness:
    def test_full_window_reports_100_pct(self):
        start = NOW - timedelta(hours=10)
        assert _completeness_pct(start, NOW, "1h", 10) == 100.0

    def test_half_window_reports_50_pct(self):
        start = NOW - timedelta(hours=10)
        assert _completeness_pct(start, NOW, "1h", 5) == 50.0

    def test_empty_result_is_not_reported_as_complete(self):
        start = NOW - timedelta(hours=10)
        assert _completeness_pct(start, NOW, "1h", 0) == 0.0

    def test_unparseable_period_degrades_gracefully(self):
        assert _completeness_pct(NOW - timedelta(hours=1), NOW, "banana", 5) == 100.0
        assert _completeness_pct(NOW - timedelta(hours=1), NOW, "banana", 0) == 0.0
