"""OTLP-exported readiness metrics."""

from __future__ import annotations

from typing import Any

from opentelemetry import metrics as _otel_metrics
from prometheus_client import Gauge

try:
    from petrosa_otel import get_meter
except ImportError:  # pragma: no cover - exercised when the shared package is absent
    get_meter = _otel_metrics.get_meter


_meter = get_meter(__name__)

LEDGER_TIEOUT_VARIANCE = _meter.create_gauge(
    "ledger_tieout_variance_usd",
    description="Latest ledger tie-out variance",
)
LEDGER_TIEOUT_CUMULATIVE = _meter.create_gauge(
    "ledger_tieout_cumulative_variance_usd",
    description="Cumulative ledger tie-out variance",
)
LEDGER_TIEOUT_LAST_COMPLETE_DAY = _meter.create_gauge(
    "ledger_tieout_last_complete_day",
    description="Last complete UTC day included in tie-out",
)
LEDGER_TIEOUT_CYCLES = _meter.create_counter(
    "ledger_tieout_cycles_total",
    description="Ledger tie-out scheduler cycles by outcome (success, failed)",
)
LEDGER_OPEN_POSITION_VARIANCE = _meter.create_gauge(
    "ledger_open_positions_variance",
    description="Ledger rows without exchange positions",
)
TRADING_STATE_MYSQL_COPY_FAILED = _meter.create_counter(
    "trading_state_mysql_copy_failed_total",
    description="Trading state MySQL copy failures",
)
TRADING_STATE_MYSQL_FIELDS_DROPPED = _meter.create_counter(
    "trading_state_mysql_fields_dropped_total",
    description="Trading state fields omitted from MySQL copies",
)
TRADING_STATE_DEAD_LETTER_COUNT = _meter.create_gauge(
    "trading_state_dead_letter_count",
    description="Trading state MySQL copy dead letters",
)
TRADING_STATE_RECOVERY_EVENTS = _meter.create_counter(
    "trading_state_recovery_events_total",
    description="Trading state replay successes, fallback failures and task failures",
)
STRATEGY_SCORECARD_EVALUATE_TOTAL = _meter.create_counter(
    "strategy_scorecard_evaluate_total",
    description="Scorecard evaluation decisions",
)
ATLAS_LOAD_CACHE_EVENTS = _meter.create_counter(
    "atlas_load_cache_events_total",
    description="Read-path cache hits and backend evaluations by route",
)
PERFORMANCE_SNAPSHOT_REFRESH = _meter.create_counter(
    "performance_snapshot_refresh_total",
    description="Strategy performance snapshot refreshes by strategy_id and outcome",
)
PERFORMANCE_REPLAY_FALLBACK = _meter.create_counter(
    "performance_snapshot_replay_fallback_total",
    description=(
        "Snapshots computed while the leg-aware replay timed out, so their outcomes come from the "
        "netting calculator (#842), by strategy_id"
    ),
)
PERFORMANCE_SNAPSHOT_AGE = _meter.create_gauge(
    "performance_snapshot_age_seconds",
    description="Age of each strategy's performance snapshot after a refresh pass",
    unit="s",
)
STARTUP_COMPONENT_UP = _meter.create_gauge(
    "startup_component_up",
    description="1 when a startup component is running, 0 when it failed or waits for the database",
)

CANDLE_READINESS_DEPTH = Gauge(
    "data_manager_candle_readiness_depth",
    "Current candle count used by the readiness gate",
    ["pair", "timeframe"],
)
CANDLE_READINESS_REQUIRED_DEPTH = Gauge(
    "data_manager_candle_readiness_required_depth",
    "Required candle count for the readiness gate",
    ["pair", "timeframe"],
)
CANDLE_READINESS_AGE_SECONDS = Gauge(
    "data_manager_candle_readiness_age_seconds",
    "Age of the newest candle used by the readiness gate",
    ["pair", "timeframe"],
)
CANDLE_READINESS_MAX_AGE_SECONDS = Gauge(
    "data_manager_candle_readiness_max_age_seconds",
    "Maximum allowed age of the newest candle",
    ["pair", "timeframe"],
)
CANDLE_STRATEGY_MINIMUM = Gauge(
    "data_manager_candle_strategy_minimum",
    "Declared minimum candle count for an active strategy",
    ["strategy", "timeframe"],
)
CANDLE_STRATEGY_DEPTH = Gauge(
    "data_manager_candle_strategy_depth",
    "Lowest observed candle count across the readiness scope for a strategy",
    ["strategy", "timeframe"],
)


def initialize_readiness_metrics() -> None:
    """Materialize known startup series after the OTLP provider is configured."""
    LEDGER_TIEOUT_VARIANCE.set(0, {"window": "latest"})
    LEDGER_TIEOUT_VARIANCE.set(0, {"window": "worst_in_window"})
    LEDGER_TIEOUT_CUMULATIVE.set(0)
    LEDGER_TIEOUT_LAST_COMPLETE_DAY.set(0)
    LEDGER_OPEN_POSITION_VARIANCE.set(0)
    TRADING_STATE_DEAD_LETTER_COUNT.set(0)
    STRATEGY_SCORECARD_EVALUATE_TOTAL.add(0, {"decision": "unconfigured"})


def metric_name(instrument: Any) -> str:
    """Return an instrument name for registration tests across OTel SDK versions."""
    return getattr(instrument, "name", getattr(instrument, "_name", ""))
