from data_manager.observability_metrics import (
    LEDGER_OPEN_POSITION_VARIANCE,
    LEDGER_TIEOUT_CUMULATIVE,
    LEDGER_TIEOUT_LAST_COMPLETE_DAY,
    LEDGER_TIEOUT_VARIANCE,
    STRATEGY_SCORECARD_EVALUATE_TOTAL,
    TRADING_STATE_DEAD_LETTER_COUNT,
    TRADING_STATE_MYSQL_COPY_FAILED,
    TRADING_STATE_MYSQL_FIELDS_DROPPED,
    TRADING_STATE_RECOVERY_EVENTS,
    metric_name,
)


def test_ledger_tieout_variance_is_registered_on_otlp_meter():
    assert metric_name(LEDGER_TIEOUT_VARIANCE) == "ledger_tieout_variance_usd"


def test_ledger_tieout_cumulative_is_registered_on_otlp_meter():
    assert (
        metric_name(LEDGER_TIEOUT_CUMULATIVE) == "ledger_tieout_cumulative_variance_usd"
    )


def test_ledger_tieout_last_complete_day_is_registered_on_otlp_meter():
    assert (
        metric_name(LEDGER_TIEOUT_LAST_COMPLETE_DAY)
        == "ledger_tieout_last_complete_day"
    )


def test_ledger_open_position_variance_is_registered_on_otlp_meter():
    assert (
        metric_name(LEDGER_OPEN_POSITION_VARIANCE) == "ledger_open_positions_variance"
    )


def test_trading_state_mysql_copy_failed_is_registered_on_otlp_meter():
    assert (
        metric_name(TRADING_STATE_MYSQL_COPY_FAILED)
        == "trading_state_mysql_copy_failed_total"
    )


def test_trading_state_mysql_fields_dropped_is_registered_on_otlp_meter():
    assert (
        metric_name(TRADING_STATE_MYSQL_FIELDS_DROPPED)
        == "trading_state_mysql_fields_dropped_total"
    )


def test_trading_state_dead_letter_count_is_registered_on_otlp_meter():
    assert (
        metric_name(TRADING_STATE_DEAD_LETTER_COUNT)
        == "trading_state_dead_letter_count"
    )


def test_trading_state_recovery_events_are_registered_on_otlp_meter():
    assert (
        metric_name(TRADING_STATE_RECOVERY_EVENTS)
        == "trading_state_recovery_events_total"
    )


def test_strategy_scorecard_evaluate_is_registered_on_otlp_meter():
    assert (
        metric_name(STRATEGY_SCORECARD_EVALUATE_TOTAL)
        == "strategy_scorecard_evaluate_total"
    )
