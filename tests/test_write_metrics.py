import json
import logging

from prometheus_client import REGISTRY

from data_manager.observability.write_metrics import (
    PETROSA_DATA_MANAGER_WRITE_DURATION_SECONDS,
    PETROSA_DATA_MANAGER_WRITES_TOTAL,
    WriteSummary,
    record_write,
)


def test_write_metrics_have_bounded_labels_and_record_event():
    assert set(PETROSA_DATA_MANAGER_WRITES_TOTAL._labelnames) == {
        "collection",
        "outcome",
        "operation",
    }
    assert set(PETROSA_DATA_MANAGER_WRITE_DURATION_SECONDS._labelnames) == {
        "collection",
        "operation",
    }
    record_write("test_collection", "success", "insert", 0.25)
    assert (
        PETROSA_DATA_MANAGER_WRITES_TOTAL.labels(
            collection="test_collection", outcome="success", operation="insert"
        )._value.get()
        >= 1
    )


def test_registered_write_metrics_do_not_have_unbounded_labels():
    names = {
        "petrosa_data_manager_writes_total",
        "petrosa_data_manager_write_duration_seconds",
    }
    for metric in REGISTRY.collect():
        if metric.name in names:
            labels = set().union(*(sample.labels for sample in metric.samples)) - {
                "le",
            }
            assert labels <= {"collection", "outcome", "operation"}


def test_summary_shape_and_debug_safe_fields(caplog):
    clock_value = [0.0]
    summary = WriteSummary(clock=lambda: clock_value[0])
    summary.write("health_metrics", "success", "insert", 0.2)
    clock_value[0] = 300.0
    with caplog.at_level(logging.INFO):
        record = summary.emit()
    assert record is not None
    assert record["event"] == "SUMMARY"
    assert record["window_seconds"] == 300
    assert record["service"] == "petrosa-data-manager"
    assert set(record) == {"event", "window_seconds", "service", "writes", "lease_ops", "latency_seconds"}
    assert json.loads(caplog.records[-1].message)["latency_seconds"]["p50"] == 0.2
