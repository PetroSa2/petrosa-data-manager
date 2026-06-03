"""Tests for the klines-retention maintenance job (petrosa_k8s#783 / data-manager#210)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from data_manager.maintenance import klines_retention as kr


def _aware(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


def test_parse_timeframe_returns_suffix_for_klines_collection():
    assert kr.parse_timeframe("klines_1h") == "1h"
    assert kr.parse_timeframe("klines_5m") == "5m"
    assert kr.parse_timeframe("klines_1d") == "1d"


def test_parse_timeframe_returns_none_for_non_klines_or_bare_prefix():
    assert kr.parse_timeframe("intents") is None
    assert kr.parse_timeframe("cio_decisions") is None
    assert kr.parse_timeframe("klines_") is None
    assert kr.parse_timeframe("not_klines_1h") is None


def test_resolve_window_days_prefers_override_then_default_then_fallback():
    overrides = {"1h": 5, "5m": 3}
    assert kr.resolve_window_days("1h", overrides) == 5
    # Falls back to DEFAULT_RETENTION_DAYS when override map is empty
    assert kr.resolve_window_days("1d", {}) == kr.DEFAULT_RETENTION_DAYS["1d"]
    # Unknown timeframe → FALLBACK
    assert kr.resolve_window_days("17h", overrides) == kr.FALLBACK_RETENTION_DAYS
    # None timeframe → FALLBACK
    assert kr.resolve_window_days(None, overrides) == kr.FALLBACK_RETENTION_DAYS


def test_compute_cutoff_subtracts_days_from_now():
    now = _aware(2026, 6, 2)
    assert kr.compute_cutoff(now, 7) == _aware(2026, 5, 26)


def test_load_config_from_env_applies_per_timeframe_overrides():
    env = {
        "KLINES_RETENTION_DAYS_1H": "14",
        "KLINES_RETENTION_DAYS_1D": "180",
        "KLINES_RETENTION_BATCH_DAYS": "3",
        "KLINES_RETENTION_MAX_CHUNKS_PER_COLLECTION": "10",
        "KLINES_RETENTION_DRY_RUN": "true",
    }
    config = kr.load_config_from_env(env)
    assert config.windows_days["1h"] == 14
    assert config.windows_days["1d"] == 180
    # Untouched timeframe keeps its default
    assert config.windows_days["1m"] == kr.DEFAULT_RETENTION_DAYS["1m"]
    assert config.batch_days == 3
    assert config.max_chunks_per_collection == 10
    assert config.dry_run is True


def test_load_config_from_env_ignores_non_integer_values_and_clamps_below_minimum():
    env = {
        "KLINES_RETENTION_DAYS_1H": "not-a-number",
        "KLINES_RETENTION_BATCH_DAYS": "0",
    }
    config = kr.load_config_from_env(env)
    assert config.windows_days["1h"] == kr.DEFAULT_RETENTION_DAYS["1h"]
    assert config.batch_days == 1


@pytest.mark.asyncio
async def test_discover_klines_collections_filters_and_sorts():
    adapter = AsyncMock()
    adapter.list_collections.return_value = [
        "intents",
        "klines_1h",
        "cio_decisions",
        "klines_5m",
        "klines_1d",
        "alerts",
    ]
    result = await kr.discover_klines_collections(adapter)
    assert result == ["klines_1d", "klines_1h", "klines_5m"]


@pytest.mark.asyncio
async def test_prune_collection_skips_when_no_eligible_docs():
    adapter = AsyncMock()
    adapter.get_record_count.return_value = 0

    result = await kr.prune_collection(
        adapter,
        "klines_1h",
        cutoff=_aware(2026, 5, 1),
        batch_days=1,
        max_chunks=100,
        dry_run=False,
    )

    assert result.docs_deleted == 0
    assert result.chunks_processed == 0
    assert result.capped is False
    adapter.delete_range.assert_not_called()


@pytest.mark.asyncio
async def test_prune_collection_walks_chunks_and_deletes_in_live_mode():
    adapter = AsyncMock()
    adapter.get_record_count.return_value = 5  # total eligible
    oldest = _aware(2026, 5, 28)
    adapter.query_range.return_value = [{"timestamp": oldest}]
    adapter.delete_range.side_effect = [2, 2, 1]

    cutoff = _aware(2026, 5, 31)
    result = await kr.prune_collection(
        adapter,
        "klines_1h",
        cutoff=cutoff,
        batch_days=1,
        max_chunks=10,
        dry_run=False,
    )

    assert result.docs_deleted == 5
    assert result.chunks_processed == 3
    assert result.capped is False
    # Three day-sized chunks: 28→29, 29→30, 30→31
    expected_windows = [
        (_aware(2026, 5, 28), _aware(2026, 5, 29)),
        (_aware(2026, 5, 29), _aware(2026, 5, 30)),
        (_aware(2026, 5, 30), _aware(2026, 5, 31)),
    ]
    actual_calls = adapter.delete_range.await_args_list
    assert len(actual_calls) == 3
    for call, (start, end) in zip(actual_calls, expected_windows, strict=True):
        assert call.args[0] == "klines_1h"
        assert call.kwargs["start"] == start
        assert call.kwargs["end"] == end


@pytest.mark.asyncio
async def test_prune_collection_dry_run_counts_without_deleting():
    adapter = AsyncMock()
    # First call: total eligible. Subsequent calls: per-chunk count.
    adapter.get_record_count.side_effect = [10, 4, 6]
    oldest = _aware(2026, 5, 29)
    adapter.query_range.return_value = [{"timestamp": oldest}]

    result = await kr.prune_collection(
        adapter,
        "klines_5m",
        cutoff=_aware(2026, 5, 31),
        batch_days=1,
        max_chunks=10,
        dry_run=True,
    )

    assert result.dry_run is True
    assert result.docs_deleted == 10
    assert result.chunks_processed == 2
    adapter.delete_range.assert_not_called()


@pytest.mark.asyncio
async def test_prune_collection_caps_at_max_chunks():
    adapter = AsyncMock()
    adapter.get_record_count.return_value = 100
    oldest = _aware(2026, 1, 1)
    adapter.query_range.return_value = [{"timestamp": oldest}]
    adapter.delete_range.return_value = 10

    result = await kr.prune_collection(
        adapter,
        "klines_1h",
        cutoff=_aware(2026, 6, 1),
        batch_days=1,
        max_chunks=3,
        dry_run=False,
    )

    assert result.chunks_processed == 3
    assert result.docs_deleted == 30
    assert result.capped is True


@pytest.mark.asyncio
async def test_prune_collection_treats_naive_timestamps_as_utc():
    adapter = AsyncMock()
    adapter.get_record_count.return_value = 1
    naive_oldest = datetime(2026, 5, 30)  # naive
    adapter.query_range.return_value = [{"timestamp": naive_oldest}]
    adapter.delete_range.return_value = 1

    result = await kr.prune_collection(
        adapter,
        "klines_1h",
        cutoff=_aware(2026, 5, 31),
        batch_days=1,
        max_chunks=10,
        dry_run=False,
    )

    assert result.docs_deleted == 1
    first_call = adapter.delete_range.await_args_list[0]
    # start should be the same instant but tz-aware (UTC)
    assert first_call.kwargs["start"].tzinfo is not None


@pytest.mark.asyncio
async def test_prune_klines_processes_all_collections_with_per_timeframe_windows():
    adapter = AsyncMock()
    adapter.list_collections.return_value = ["klines_1h", "klines_1d", "intents"]
    # discover_klines_collections sorts alphabetically → klines_1d processed
    # first, then klines_1h. klines_1d has nothing eligible; klines_1h walks
    # two day-sized chunks that each delete one doc.
    adapter.get_record_count.side_effect = [0, 2]
    adapter.query_range.return_value = [{"timestamp": _aware(2026, 5, 30)}]
    adapter.delete_range.side_effect = [1, 1]

    config = kr.RetentionConfig(
        windows_days={"1h": 1, "1d": 30},
        batch_days=1,
        max_chunks_per_collection=10,
        dry_run=False,
    )
    now = _aware(2026, 6, 2)

    results = await kr.prune_klines(adapter, config, now=now)

    assert [r.collection for r in results] == ["klines_1d", "klines_1h"]
    by_collection = {r.collection: r for r in results}
    assert by_collection["klines_1h"].cutoff == _aware(2026, 6, 1)
    assert by_collection["klines_1d"].cutoff == _aware(2026, 5, 3)
    assert by_collection["klines_1h"].docs_deleted == 2
    assert by_collection["klines_1d"].docs_deleted == 0


@pytest.mark.asyncio
async def test_prune_klines_uses_collections_override_when_provided():
    adapter = AsyncMock()
    adapter.get_record_count.return_value = 0

    config = kr.RetentionConfig(
        windows_days={},
        batch_days=1,
        max_chunks_per_collection=10,
        dry_run=False,
        collections_override=["klines_15m"],
    )

    results = await kr.prune_klines(adapter, config, now=_aware(2026, 6, 2))

    adapter.list_collections.assert_not_called()
    assert [r.collection for r in results] == ["klines_15m"]
