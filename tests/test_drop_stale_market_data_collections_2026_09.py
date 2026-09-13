"""Unit tests for
`data_manager.maintenance.drop_stale_market_data_collections_2026_09`
(data-manager#273 AC2/AC3).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from data_manager.maintenance import (
    drop_stale_market_data_collections_2026_09 as mod,
)


def _now() -> datetime:
    return datetime(2026, 9, 13, tzinfo=UTC)


def _days_ago(days: float, *, now: datetime | None = None) -> datetime:
    return (now or _now()) - timedelta(days=days)


class TestClassifyCollection:
    def test_tickers_symbol(self):
        assert mod.classify_collection("tickers_BTCUSDT") == mod.CATEGORY_TICKERS_SYMBOL
        assert mod.classify_collection("tickers_ETHUSDT") == mod.CATEGORY_TICKERS_SYMBOL

    def test_trades_symbol(self):
        assert mod.classify_collection("trades_BTCUSDT") == mod.CATEGORY_TRADES_SYMBOL

    def test_plain_trades(self):
        assert mod.classify_collection("trades") == mod.CATEGORY_TRADES_PLAIN

    def test_non_matching_returns_none(self):
        assert mod.classify_collection("candles_BTCUSDT_1h") is None
        assert mod.classify_collection("klines_1h") is None
        assert mod.classify_collection("intents") is None
        assert mod.classify_collection("tickers_") is None
        assert mod.classify_collection("trades_") is None
        assert mod.classify_collection("datasets") is None
        assert mod.classify_collection("lineage_records") is None


class TestDiscoverTargetCollections:
    @pytest.mark.asyncio
    async def test_filters_and_sorts(self):
        adapter = AsyncMock()
        adapter.list_collections.return_value = [
            "intents",
            "tickers_ETHUSDT",
            "candles_BTCUSDT_1h",
            "trades_BTCUSDT",
            "trades",
            "klines_1h",
            "tickers_BTCUSDT",
        ]
        result = await mod.discover_target_collections(adapter)
        assert result == [
            "tickers_BTCUSDT",
            "tickers_ETHUSDT",
            "trades",
            "trades_BTCUSDT",
        ]


class TestProcessCollectionTickersSymbol:
    """tickers_{symbol} — confirmed dead, no reader, default apply target."""

    @pytest.mark.asyncio
    async def test_dry_run_reports_would_drop_when_stale_enough(self):
        adapter = AsyncMock()
        adapter.get_record_count.return_value = 89
        adapter.query_latest.return_value = [{"timestamp": _days_ago(330)}]

        result = await mod.process_collection(
            adapter,
            "tickers_BTCUSDT",
            dry_run=True,
            min_age_days=30,
            include_wired_reader=False,
            now=_now(),
        )

        assert result.category == mod.CATEGORY_TICKERS_SYMBOL
        assert result.existed is True
        assert result.doc_count == 89
        assert result.guard_tripped is False
        assert result.dropped is False

    @pytest.mark.asyncio
    async def test_apply_drops_when_stale_enough(self):
        adapter = AsyncMock()
        adapter.get_record_count.return_value = 89
        adapter.query_latest.return_value = [{"timestamp": _days_ago(330)}]
        drop_mock = AsyncMock()
        adapter.db = {"tickers_BTCUSDT": MagicMock(drop=drop_mock)}

        result = await mod.process_collection(
            adapter,
            "tickers_BTCUSDT",
            dry_run=False,
            min_age_days=30,
            include_wired_reader=False,
            now=_now(),
        )

        assert result.dropped is True
        assert result.guard_tripped is False
        drop_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_apply_refuses_when_recently_written(self):
        adapter = AsyncMock()
        adapter.get_record_count.return_value = 89
        adapter.query_latest.return_value = [{"timestamp": _days_ago(2)}]
        adapter.db = {"tickers_BTCUSDT": MagicMock(drop=AsyncMock())}

        result = await mod.process_collection(
            adapter,
            "tickers_BTCUSDT",
            dry_run=False,
            min_age_days=30,
            include_wired_reader=False,
            now=_now(),
        )

        assert result.dropped is False
        assert result.guard_tripped is True
        adapter.db["tickers_BTCUSDT"].drop.assert_not_called()

    @pytest.mark.asyncio
    async def test_absent_collection_is_noop(self):
        adapter = AsyncMock()
        adapter.get_record_count.return_value = 0
        adapter.list_collections.return_value = []

        result = await mod.process_collection(
            adapter,
            "tickers_ADAUSDT",
            dry_run=True,
            min_age_days=30,
            include_wired_reader=False,
            now=_now(),
        )

        assert result.existed is False
        assert result.dropped is False
        assert result.guard_tripped is False
        adapter.query_latest.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_but_existing_collection_guard_trips(self):
        """Zero docs but the name is present elsewhere (edge case): no
        usable newest-doc timestamp -> guard trips, never silently drops."""
        adapter = AsyncMock()
        adapter.get_record_count.return_value = 0
        adapter.list_collections.return_value = ["tickers_ADAUSDT"]
        adapter.query_latest.return_value = []

        result = await mod.process_collection(
            adapter,
            "tickers_ADAUSDT",
            dry_run=False,
            min_age_days=30,
            include_wired_reader=False,
            now=_now(),
        )

        assert result.existed is True
        assert result.guard_tripped is True
        assert result.dropped is False

    @pytest.mark.asyncio
    async def test_never_raises_on_count_error(self):
        adapter = AsyncMock()
        adapter.get_record_count.side_effect = Exception("boom")

        result = await mod.process_collection(
            adapter,
            "tickers_BTCUSDT",
            dry_run=False,
            min_age_days=30,
            include_wired_reader=False,
            now=_now(),
        )

        assert result.dropped is False
        assert result.doc_count == 0

    @pytest.mark.asyncio
    async def test_never_raises_on_query_latest_error(self):
        adapter = AsyncMock()
        adapter.get_record_count.return_value = 89
        adapter.query_latest.side_effect = Exception("boom")

        result = await mod.process_collection(
            adapter,
            "tickers_BTCUSDT",
            dry_run=False,
            min_age_days=30,
            include_wired_reader=False,
            now=_now(),
        )

        assert result.dropped is False
        assert result.guard_tripped is True


class TestProcessCollectionTradesSymbol:
    """trades_{symbol} — wired reader (GET /api/v1/data/trades); retained
    unless --include-wired-reader, per the AC1 adversarial-review correction."""

    @pytest.mark.asyncio
    async def test_retained_by_default(self):
        adapter = AsyncMock()

        result = await mod.process_collection(
            adapter,
            "trades_BTCUSDT",
            dry_run=True,
            min_age_days=30,
            include_wired_reader=False,
            now=_now(),
        )

        assert result.category == mod.CATEGORY_TRADES_SYMBOL
        assert result.retained_wired_reader is True
        assert result.dropped is False
        adapter.get_record_count.assert_not_called()
        adapter.query_latest.assert_not_called()

    @pytest.mark.asyncio
    async def test_processed_when_opted_in_and_stale(self):
        adapter = AsyncMock()
        adapter.get_record_count.return_value = 824
        adapter.query_latest.return_value = [{"timestamp": _days_ago(330)}]
        adapter.db = {"trades_BTCUSDT": MagicMock(drop=AsyncMock())}

        result = await mod.process_collection(
            adapter,
            "trades_BTCUSDT",
            dry_run=False,
            min_age_days=30,
            include_wired_reader=True,
            now=_now(),
        )

        assert result.retained_wired_reader is False
        assert result.dropped is True

    @pytest.mark.asyncio
    async def test_opted_in_but_recently_written_still_guarded(self):
        adapter = AsyncMock()
        adapter.get_record_count.return_value = 824
        adapter.query_latest.return_value = [{"timestamp": _days_ago(1)}]

        result = await mod.process_collection(
            adapter,
            "trades_BTCUSDT",
            dry_run=False,
            min_age_days=30,
            include_wired_reader=True,
            now=_now(),
        )

        assert result.guard_tripped is True
        assert result.dropped is False


class TestProcessCollectionPlainTrades:
    """plain `trades` — retirement already decided
    (intents_ttl_index.py SIBLING_COLLECTIONS history); default apply target."""

    @pytest.mark.asyncio
    async def test_dropped_when_stale_enough(self):
        adapter = AsyncMock()
        adapter.get_record_count.return_value = 53_825
        adapter.query_latest.return_value = [{"timestamp": _days_ago(34)}]
        adapter.db = {"trades": MagicMock(drop=AsyncMock())}

        result = await mod.process_collection(
            adapter,
            "trades",
            dry_run=False,
            min_age_days=30,
            include_wired_reader=False,
            now=_now(),
        )

        assert result.category == mod.CATEGORY_TRADES_PLAIN
        assert result.dropped is True

    @pytest.mark.asyncio
    async def test_guard_trips_when_written_within_window(self):
        adapter = AsyncMock()
        adapter.get_record_count.return_value = 53_825
        adapter.query_latest.return_value = [{"timestamp": _days_ago(3)}]

        result = await mod.process_collection(
            adapter,
            "trades",
            dry_run=False,
            min_age_days=30,
            include_wired_reader=False,
            now=_now(),
        )

        assert result.guard_tripped is True
        assert result.dropped is False


class TestExecuteMigration:
    @pytest.mark.asyncio
    async def test_discovers_and_processes_all_categories(self):
        adapter = AsyncMock()
        adapter.list_collections.return_value = [
            "tickers_BTCUSDT",
            "trades_BTCUSDT",
            "trades",
            "candles_BTCUSDT_1h",
        ]
        adapter.get_record_count.return_value = 10
        adapter.query_latest.return_value = [{"timestamp": _days_ago(330)}]

        results = await mod.execute_migration(adapter, dry_run=True, min_age_days=30)

        by_name = {r.collection: r for r in results}
        assert set(by_name) == {"tickers_BTCUSDT", "trades_BTCUSDT", "trades"}
        assert by_name["trades_BTCUSDT"].retained_wired_reader is True

    @pytest.mark.asyncio
    async def test_explicit_collections_override_skips_discovery(self):
        adapter = AsyncMock()
        adapter.get_record_count.return_value = 10
        adapter.query_latest.return_value = [{"timestamp": _days_ago(330)}]

        results = await mod.execute_migration(
            adapter,
            dry_run=True,
            min_age_days=30,
            collections=["tickers_BTCUSDT"],
        )

        adapter.list_collections.assert_not_called()
        assert [r.collection for r in results] == ["tickers_BTCUSDT"]


class TestResolveMinAgeDays:
    def test_cli_value_wins(self, monkeypatch):
        monkeypatch.setenv("MARKET_DATA_STALE_MIN_AGE_DAYS", "10")
        assert mod._resolve_min_age_days(5) == 5

    def test_env_used_when_no_cli_value(self, monkeypatch):
        monkeypatch.setenv("MARKET_DATA_STALE_MIN_AGE_DAYS", "10")
        assert mod._resolve_min_age_days(None) == 10

    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("MARKET_DATA_STALE_MIN_AGE_DAYS", raising=False)
        assert mod._resolve_min_age_days(None) == mod.DEFAULT_MIN_AGE_DAYS

    def test_ignores_non_integer(self, monkeypatch):
        monkeypatch.setenv("MARKET_DATA_STALE_MIN_AGE_DAYS", "not-a-number")
        assert mod._resolve_min_age_days(None) == mod.DEFAULT_MIN_AGE_DAYS

    def test_clamps_negative_to_zero(self, monkeypatch):
        monkeypatch.delenv("MARKET_DATA_STALE_MIN_AGE_DAYS", raising=False)
        assert mod._resolve_min_age_days(-5) == 0


class TestBuildArgparser:
    def test_parses_all_flags(self):
        parser = mod._build_argparser()
        args = parser.parse_args(
            [
                "--apply",
                "--collection",
                "tickers_BTCUSDT",
                "--collection",
                "tickers_ETHUSDT",
                "--min-age-days",
                "45",
                "--include-wired-reader",
            ]
        )
        assert args.apply is True
        assert args.collections == ["tickers_BTCUSDT", "tickers_ETHUSDT"]
        assert args.min_age_days == 45
        assert args.include_wired_reader is True

    def test_requires_mode_flag(self):
        parser = mod._build_argparser()
        with pytest.raises(SystemExit) as ei:
            parser.parse_args([])
        assert ei.value.code == 2

    def test_defaults(self):
        parser = mod._build_argparser()
        args = parser.parse_args(["--dry-run"])
        assert args.collections is None
        assert args.min_age_days is None
        assert args.include_wired_reader is False


class TestAmain:
    @pytest.mark.asyncio
    async def test_returns_2_when_mongodb_url_unset(self, monkeypatch):
        monkeypatch.delenv("MONGODB_URL", raising=False)
        rc = await mod._amain(["--dry-run"])
        assert rc == 2

    @pytest.mark.asyncio
    async def test_connects_runs_and_disconnects(self, monkeypatch):
        monkeypatch.setenv("MONGODB_URL", "mongodb://localhost:27017/test")
        fake_adapter = MagicMock()
        fake_adapter.connect = MagicMock()
        fake_adapter.disconnect = MagicMock()

        with (
            patch.object(
                mod, "MongoDBAdapter", return_value=fake_adapter
            ) as adapter_cls,
            patch.object(
                mod, "execute_migration", new=AsyncMock(return_value=[])
            ) as migrate_mock,
        ):
            rc = await mod._amain(["--dry-run", "--collection", "tickers_BTCUSDT"])

        assert rc == 0
        adapter_cls.assert_called_once_with(
            connection_string="mongodb://localhost:27017/test"
        )
        fake_adapter.connect.assert_called_once()
        fake_adapter.disconnect.assert_called_once()
        migrate_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_3_when_apply_and_guard_tripped(self, monkeypatch):
        monkeypatch.setenv("MONGODB_URL", "mongodb://localhost:27017/test")
        fake_adapter = MagicMock()
        guarded_result = mod.CollectionDropResult(
            collection="trades",
            category=mod.CATEGORY_TRADES_PLAIN,
            dry_run=False,
            guard_tripped=True,
        )

        with (
            patch.object(mod, "MongoDBAdapter", return_value=fake_adapter),
            patch.object(
                mod, "execute_migration", new=AsyncMock(return_value=[guarded_result])
            ),
        ):
            rc = await mod._amain(["--apply"])

        assert rc == 3

    @pytest.mark.asyncio
    async def test_disconnects_even_when_migration_raises(self, monkeypatch):
        monkeypatch.setenv("MONGODB_URL", "mongodb://localhost:27017/test")
        fake_adapter = MagicMock()

        with (
            patch.object(mod, "MongoDBAdapter", return_value=fake_adapter),
            patch.object(
                mod,
                "execute_migration",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
        ):
            rc = await mod._amain(["--dry-run"])

        assert rc == 4
        fake_adapter.disconnect.assert_called_once()


def test_main_delegates_to_amain(monkeypatch):
    monkeypatch.delenv("MONGODB_URL", raising=False)
    rc = mod.main(["--dry-run"])
    assert rc == 2
