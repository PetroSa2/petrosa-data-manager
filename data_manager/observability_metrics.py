"""OTLP-exported readiness metrics."""

from __future__ import annotations

from typing import Any

from opentelemetry import metrics as _otel_metrics

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
STRATEGY_SCORECARD_EVALUATE_TOTAL = _meter.create_counter(
    "strategy_scorecard_evaluate_total",
    description="Scorecard evaluation decisions",
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
