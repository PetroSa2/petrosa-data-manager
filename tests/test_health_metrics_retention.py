"""Tests for health_metrics retention maintenance job (data-manager#220)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from data_manager.maintenance import health_metrics_retention as hmr


def _aware(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


def test_compute_cutoff_subtracts_days_from_now():
    now = _aware(2026, 6, 10)
    assert hmr.compute_cutoff(now, 90) == _aware(2026, 3, 12)


def test_load_config_from_env_defaults_when_empty():
    config = hmr.load_config_from_env({})
    assert config.retention_days == hmr.DEFAULT_RETENTION_DAYS
    assert config.dry_run is False


def test_load_config_from_env_respects_retention_days_override():
    config = hmr.load_config_from_env({"HEALTH_METRICS_RETENTION_DAYS": "30"})
    assert config.retention_days == 30


def test_load_config_from_env_respects_dry_run_flag():
    config = hmr.load_config_from_env({"HEALTH_METRICS_RETENTION_DRY_RUN": "true"})
    assert config.dry_run is True


def test_load_config_from_env_ignores_non_integer_retention_days():
    config = hmr.load_config_from_env({"HEALTH_METRICS_RETENTION_DAYS": "not-a-number"})
    assert config.retention_days == hmr.DEFAULT_RETENTION_DAYS


def test_load_config_from_env_clamps_retention_days_below_minimum():
    config = hmr.load_config_from_env({"HEALTH_METRICS_RETENTION_DAYS": "0"})
    assert config.retention_days == 1


def test_prune_health_metrics_dry_run_calls_get_record_count_not_delete():
    adapter = MagicMock()
    adapter.get_record_count.return_value = 42

    config = hmr.RetentionConfig(retention_days=90, dry_run=True)
    now = _aware(2026, 6, 10)
    result = hmr.prune_health_metrics(adapter, config, now=now)

    expected_cutoff = hmr.compute_cutoff(now, 90)
    adapter.get_record_count.assert_called_once_with(
        hmr.HEALTH_METRICS_TABLE, end=expected_cutoff
    )
    adapter.delete_range.assert_not_called()
    assert result.rows_deleted == 42
    assert result.dry_run is True
    assert result.table == hmr.HEALTH_METRICS_TABLE
    assert result.cutoff == expected_cutoff


def test_prune_health_metrics_delete_calls_delete_range():
    adapter = MagicMock()
    adapter.delete_range.return_value = 1500

    config = hmr.RetentionConfig(retention_days=90, dry_run=False)
    now = _aware(2026, 6, 10)
    result = hmr.prune_health_metrics(adapter, config, now=now)

    expected_cutoff = hmr.compute_cutoff(now, 90)
    adapter.delete_range.assert_called_once_with(
        hmr.HEALTH_METRICS_TABLE, start=hmr._EPOCH, end=expected_cutoff
    )
    adapter.get_record_count.assert_not_called()
    assert result.rows_deleted == 1500
    assert result.dry_run is False


def test_prune_health_metrics_zero_rows_is_valid():
    adapter = MagicMock()
    adapter.delete_range.return_value = 0

    config = hmr.RetentionConfig(retention_days=90, dry_run=False)
    result = hmr.prune_health_metrics(adapter, config, now=_aware(2026, 6, 10))

    assert result.rows_deleted == 0


def test_prune_health_metrics_uses_wall_clock_when_now_not_provided():
    adapter = MagicMock()
    adapter.delete_range.return_value = 0

    config = hmr.RetentionConfig(retention_days=90, dry_run=False)
    before = datetime.now(UTC)
    result = hmr.prune_health_metrics(adapter, config)
    after = datetime.now(UTC)

    expected_min = hmr.compute_cutoff(before, 90)
    expected_max = hmr.compute_cutoff(after, 90)
    assert expected_min <= result.cutoff <= expected_max
