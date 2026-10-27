from data_manager.observability import mongo_metrics
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


def test_atlas_metrics_are_registered():
    assert mongo_metrics.metric_name(mongo_metrics.ATLAS_OPERATIONS) == (
        "data_manager_atlas_operations_total"
    )
    assert mongo_metrics.metric_name(mongo_metrics.ATLAS_FAILURES) == (
        "data_manager_atlas_operation_failures_total"
    )
    assert mongo_metrics.metric_name(mongo_metrics.ATLAS_LATENCY) == (
        "data_manager_atlas_operation_duration_seconds"
    )


def test_atlas_listener_records_success_with_route_source(monkeypatch):
    recorded = []

    class Metric:
        def add(self, value, attributes):
            recorded.append((value, attributes))

        def record(self, value, attributes):
            recorded.append((value, attributes))

    monkeypatch.setattr(mongo_metrics, "ATLAS_OPERATIONS", Metric())
    monkeypatch.setattr(mongo_metrics, "ATLAS_LATENCY", Metric())
    listener = mongo_metrics.AtlasCommandListener("primary")
    token = mongo_metrics.set_source("/api/v1/analysis/scorecard")
    try:
        listener.command_started(
            type(
                "Event",
                (),
                {
                    "request_id": 1,
                    "command_name": "find",
                    "command": {"find": "signals"},
                },
            )()
        )
        listener.command_succeeded(
            type(
                "Event",
                (),
                {"request_id": 1, "command_name": "find", "duration_micros": 1000},
            )()
        )
    finally:
        mongo_metrics.reset_source(token)
    assert recorded == [
        (
            1,
            {
                "command": "find",
                "collection": "signals",
                "source": "/api/v1/analysis/scorecard",
            },
        ),
        (
            0.001,
            {
                "command": "find",
                "collection": "signals",
                "source": "/api/v1/analysis/scorecard",
            },
        ),
    ]


def test_atlas_listener_records_failure_and_unknown_collection(monkeypatch):
    recorded = []

    class Metric:
        def add(self, value, attributes):
            recorded.append(("add", value, attributes))

        def record(self, value, attributes):
            recorded.append(("record", value, attributes))

    monkeypatch.setattr(mongo_metrics, "ATLAS_OPERATIONS", Metric())
    monkeypatch.setattr(mongo_metrics, "ATLAS_FAILURES", Metric())
    monkeypatch.setattr(mongo_metrics, "ATLAS_LATENCY", Metric())
    listener = mongo_metrics.AtlasCommandListener("readiness")
    listener.command_started(
        type("Event", (), {"request_id": 2, "command_name": "getMore", "command": {}})()
    )
    listener.command_failed(
        type(
            "Event",
            (),
            {"request_id": 2, "command_name": "getMore", "duration_micros": 2000},
        )()
    )
    assert recorded[0][2] == {
        "command": "getMore",
        "collection": "unknown",
        "source": "readiness",
    }
    assert recorded[1][0] == "add"
    assert recorded[2][0] == "record"


def test_atlas_listener_isolates_metric_errors(monkeypatch):
    class BrokenMetric:
        def add(self, *_args):
            raise RuntimeError("metric failure")

    monkeypatch.setattr(mongo_metrics, "ATLAS_OPERATIONS", BrokenMetric())
    listener = mongo_metrics.AtlasCommandListener("primary")
    listener.command_succeeded(
        type(
            "Event", (), {"request_id": 3, "command_name": "ping", "duration_micros": 1}
        )()
    )
    assert listener._commands == {}
