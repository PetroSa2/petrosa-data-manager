"""Tests for the candles_* retention maintenance job (data-manager#274 AC3)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from data_manager.maintenance import candles_retention as cr


def _aware(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


def test_parse_candles_collection_splits_symbol_and_timeframe():
    assert cr.parse_candles_collection("candles_BTCUSDT_1h") == ("BTCUSDT", "1h")
    assert cr.parse_candles_collection("candles_ETHUSDT_15m") == ("ETHUSDT", "15m")


def test_parse_candles_collection_returns_none_for_non_matching_names():
    assert cr.parse_candles_collection("klines_1h") is None
    assert cr.parse_candles_collection("candles_") is None
    assert cr.parse_candles_collection("candles_BTCUSDT") is None
    assert cr.parse_candles_collection("intents") is None


def test_resolve_max_count_defaults_to_warmup_min_candles(monkeypatch):
    monkeypatch.delenv("CANDLES_RETENTION_MAX_COUNT", raising=False)
    assert cr._resolve_max_count() == cr.DEFAULT_MAX_CANDLES


def test_resolve_max_count_applies_override(monkeypatch):
    monkeypatch.setenv("CANDLES_RETENTION_MAX_COUNT", "800")
    assert cr._resolve_max_count() == 800


def test_resolve_max_count_ignores_non_integer_and_clamps_below_minimum(monkeypatch):
    monkeypatch.setenv("CANDLES_RETENTION_MAX_COUNT", "not-a-number")
    assert cr._resolve_max_count() == cr.DEFAULT_MAX_CANDLES

    monkeypatch.setenv("CANDLES_RETENTION_MAX_COUNT", "0")
    assert cr._resolve_max_count() == 1


@pytest.mark.asyncio
async def test_discover_candles_collections_filters_and_sorts():
    adapter = AsyncMock()
    adapter.list_collections.return_value = [
        "intents",
        "candles_BTCUSDT_1h",
        "klines_1h",
        "candles_BTCUSDT_5m",
        "candles_ADAUSDT_1d",
        "alerts",
    ]
    result = await cr.discover_candles_collections(adapter)
    assert result == [
        "candles_ADAUSDT_1d",
        "candles_BTCUSDT_1h",
        "candles_BTCUSDT_5m",
    ]


@pytest.mark.asyncio
async def test_prune_candles_collection_skips_when_under_cap():
    adapter = AsyncMock()
    adapter.get_record_count.return_value = 100

    result = await cr.prune_candles_collection(
        adapter, "candles_BTCUSDT_1h", max_count=400, dry_run=False
    )

    assert result.docs_deleted == 0
    assert result.count_before == 100
    adapter.query_latest.assert_not_called()
    adapter.delete_range.assert_not_called()


@pytest.mark.asyncio
async def test_prune_candles_collection_deletes_beyond_cap_in_live_mode():
    adapter = AsyncMock()
    adapter.get_record_count.return_value = 500
    boundary = _aware(2026, 6, 1)
    # query_latest returns newest-first; the last item is the Nth-newest
    # (the cutoff boundary).
    adapter.query_latest.return_value = [
        {"timestamp": _aware(2026, 6, 2)},
        {"timestamp": boundary},
    ]
    adapter.delete_range.return_value = 100

    result = await cr.prune_candles_collection(
        adapter, "candles_BTCUSDT_1h", max_count=400, dry_run=False
    )

    assert result.docs_deleted == 100
    assert result.count_before == 500
    adapter.query_latest.assert_awaited_once_with("candles_BTCUSDT_1h", limit=400)
    adapter.delete_range.assert_awaited_once_with(
        "candles_BTCUSDT_1h", start=cr._EPOCH, end=boundary
    )


@pytest.mark.asyncio
async def test_prune_candles_collection_dry_run_counts_without_deleting():
    adapter = AsyncMock()
    boundary = _aware(2026, 6, 1)
    adapter.get_record_count.side_effect = [500, 100]
    adapter.query_latest.return_value = [{"timestamp": boundary}]

    result = await cr.prune_candles_collection(
        adapter, "candles_BTCUSDT_1h", max_count=400, dry_run=True
    )

    assert result.docs_deleted == 100
    assert result.dry_run is True
    adapter.delete_range.assert_not_called()


@pytest.mark.asyncio
async def test_prune_candles_collection_treats_naive_timestamp_as_utc():
    adapter = AsyncMock()
    adapter.get_record_count.return_value = 500
    naive_boundary = datetime(2026, 6, 1)  # naive
    adapter.query_latest.return_value = [{"timestamp": naive_boundary}]
    adapter.delete_range.return_value = 42

    result = await cr.prune_candles_collection(
        adapter, "candles_BTCUSDT_1h", max_count=400, dry_run=False
    )

    assert result.docs_deleted == 42
    call = adapter.delete_range.await_args
    assert call.kwargs["end"].tzinfo is not None


@pytest.mark.asyncio
async def test_prune_candles_collection_handles_empty_query_latest_gracefully():
    adapter = AsyncMock()
    adapter.get_record_count.return_value = 500
    adapter.query_latest.return_value = []

    result = await cr.prune_candles_collection(
        adapter, "candles_BTCUSDT_1h", max_count=400, dry_run=False
    )

    assert result.docs_deleted == 0
    adapter.delete_range.assert_not_called()


@pytest.mark.asyncio
async def test_prune_candles_collection_never_raises_on_backend_error():
    adapter = AsyncMock()
    adapter.get_record_count.side_effect = Exception("boom")

    result = await cr.prune_candles_collection(
        adapter, "candles_BTCUSDT_1h", max_count=400, dry_run=False
    )

    assert result.docs_deleted == 0
    assert result.count_before == 0


@pytest.mark.asyncio
async def test_prune_candles_collection_never_raises_on_query_latest_error():
    adapter = AsyncMock()
    adapter.get_record_count.return_value = 500
    adapter.query_latest.side_effect = Exception("boom")

    result = await cr.prune_candles_collection(
        adapter, "candles_BTCUSDT_1h", max_count=400, dry_run=False
    )

    assert result.docs_deleted == 0
    assert result.count_before == 500
    adapter.delete_range.assert_not_called()


@pytest.mark.asyncio
async def test_prune_candles_collection_skips_when_boundary_not_datetime():
    adapter = AsyncMock()
    adapter.get_record_count.return_value = 500
    adapter.query_latest.return_value = [{"timestamp": "not-a-datetime"}]

    result = await cr.prune_candles_collection(
        adapter, "candles_BTCUSDT_1h", max_count=400, dry_run=False
    )

    assert result.docs_deleted == 0
    adapter.delete_range.assert_not_called()


@pytest.mark.asyncio
async def test_prune_candles_collection_never_raises_on_delete_range_error():
    adapter = AsyncMock()
    adapter.get_record_count.return_value = 500
    adapter.query_latest.return_value = [{"timestamp": _aware(2026, 6, 1)}]
    adapter.delete_range.side_effect = Exception("boom")

    result = await cr.prune_candles_collection(
        adapter, "candles_BTCUSDT_1h", max_count=400, dry_run=False
    )

    assert result.docs_deleted == 0
    assert result.count_before == 500


@pytest.mark.asyncio
async def test_prune_candles_processes_all_discovered_collections():
    adapter = AsyncMock()
    adapter.list_collections.return_value = [
        "candles_BTCUSDT_1h",
        "candles_ETHUSDT_1h",
        "intents",
    ]
    adapter.get_record_count.return_value = 100  # under cap for both

    results = await cr.prune_candles(adapter, max_count=400, dry_run=False)

    assert [r.collection for r in results] == [
        "candles_BTCUSDT_1h",
        "candles_ETHUSDT_1h",
    ]
    assert all(r.docs_deleted == 0 for r in results)


@pytest.mark.asyncio
async def test_prune_candles_uses_collections_override_when_provided():
    adapter = AsyncMock()
    adapter.get_record_count.return_value = 0

    results = await cr.prune_candles(
        adapter,
        max_count=400,
        dry_run=False,
        collections_override=["candles_BTCUSDT_15m"],
    )

    adapter.list_collections.assert_not_called()
    assert [r.collection for r in results] == ["candles_BTCUSDT_15m"]


@pytest.mark.asyncio
async def test_prune_candles_returns_empty_list_when_no_collections_found():
    adapter = AsyncMock()
    adapter.list_collections.return_value = ["intents", "klines_1h"]

    results = await cr.prune_candles(adapter, max_count=400, dry_run=False)

    assert results == []
    adapter.get_record_count.assert_not_called()


@pytest.mark.asyncio
async def test_prune_candles_defaults_max_count_from_env(monkeypatch):
    monkeypatch.setenv("CANDLES_RETENTION_MAX_COUNT", "50")
    adapter = AsyncMock()
    adapter.get_record_count.return_value = 10

    results = await cr.prune_candles(
        adapter, dry_run=False, collections_override=["candles_BTCUSDT_1h"]
    )

    assert results[0].max_count == 50


def test_build_argparser_parses_all_flags():
    parser = cr._build_argparser()
    args = parser.parse_args(
        [
            "--dry-run",
            "--collections",
            "candles_BTCUSDT_1h,candles_ETHUSDT_1h",
            "--max-count",
            "123",
        ]
    )
    assert args.dry_run is True
    assert args.collections == "candles_BTCUSDT_1h,candles_ETHUSDT_1h"
    assert args.max_count == 123


def test_build_argparser_defaults():
    parser = cr._build_argparser()
    args = parser.parse_args([])
    assert args.dry_run is False
    assert args.collections is None
    assert args.max_count is None


def test_configure_logging_resolves_level_from_env(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    with patch.object(cr.logging, "basicConfig") as basic_config:
        cr._configure_logging()
    assert basic_config.call_args.kwargs["level"] == cr.logging.DEBUG


@pytest.mark.asyncio
async def test_amain_returns_2_when_mongodb_url_unset(monkeypatch):
    monkeypatch.delenv("MONGODB_URL", raising=False)
    rc = await cr._amain([])
    assert rc == 2


@pytest.mark.asyncio
async def test_amain_connects_prunes_and_disconnects(monkeypatch):
    monkeypatch.setenv("MONGODB_URL", "mongodb://localhost:27017/test")
    fake_adapter = MagicMock()
    fake_adapter.connect = MagicMock()
    fake_adapter.disconnect = MagicMock()

    with (
        patch.object(cr, "MongoDBAdapter", return_value=fake_adapter) as adapter_cls,
        patch.object(cr, "prune_candles", new=AsyncMock(return_value=[])) as prune_mock,
    ):
        rc = await cr._amain(
            ["--dry-run", "--collections", "candles_BTCUSDT_1h", "--max-count", "10"]
        )

    assert rc == 0
    adapter_cls.assert_called_once_with(
        connection_string="mongodb://localhost:27017/test"
    )
    fake_adapter.connect.assert_called_once()
    fake_adapter.disconnect.assert_called_once()
    prune_mock.assert_awaited_once_with(
        fake_adapter,
        max_count=10,
        dry_run=True,
        collections_override=["candles_BTCUSDT_1h"],
    )


@pytest.mark.asyncio
async def test_amain_disconnects_even_when_prune_raises(monkeypatch):
    monkeypatch.setenv("MONGODB_URL", "mongodb://localhost:27017/test")
    fake_adapter = MagicMock()

    with (
        patch.object(cr, "MongoDBAdapter", return_value=fake_adapter),
        patch.object(
            cr, "prune_candles", new=AsyncMock(side_effect=RuntimeError("boom"))
        ),
        pytest.raises(RuntimeError),
    ):
        await cr._amain([])

    fake_adapter.disconnect.assert_called_once()


def test_main_delegates_to_amain(monkeypatch):
    monkeypatch.setenv("MONGODB_URL", "")
    monkeypatch.delenv("MONGODB_URL", raising=False)
    rc = cr.main([])
    assert rc == 2
