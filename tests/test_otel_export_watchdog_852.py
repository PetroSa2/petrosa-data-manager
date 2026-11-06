"""OTLP metric export watchdog (petrosa-data-manager#852)."""

from __future__ import annotations

import tempfile
import threading

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    MetricExporter,
    MetricExportResult,
    PeriodicExportingMetricReader,
)

from data_manager.observability import otel_export_watchdog as wd


class _Exporter(MetricExporter):
    def __init__(self) -> None:
        super().__init__()
        self.result = MetricExportResult.SUCCESS
        self.calls = 0

    def export(self, metrics_data, timeout_millis: float = 10_000, **kwargs):
        self.calls += 1
        return self.result

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return True

    def shutdown(self, timeout_millis: float = 30_000, **kwargs) -> None:
        return None


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class _Reader:
    def __init__(self, exporter, interval_ms: int = 60_000) -> None:
        self._exporter = exporter
        self._export_interval_millis = interval_ms


def test_successful_exports_are_timestamped_failures_are_not() -> None:
    clock, exporter = _Clock(), _Exporter()
    watch = wd.ExportWatch(_Reader(exporter), clock=clock)
    clock.t = 50.0
    assert exporter.export(None) is MetricExportResult.SUCCESS  # behaviour unchanged
    assert watch.last_success == 50.0
    exporter.result = MetricExportResult.FAILURE
    clock.t = 120.0
    exporter.export(None)
    assert watch.last_success == 50.0
    assert exporter.calls == 2


def test_stall_after_three_intervals_is_reported_once_then_recovers() -> None:
    clock, exporter = _Clock(), _Exporter()
    watch = wd.ExportWatch(_Reader(exporter, interval_ms=60_000), clock=clock)
    clock.t = 179.0
    assert watch.check() is False
    clock.t = 181.0
    assert watch.check() is True  # first check past 3 x 60 s
    clock.t = 400.0
    assert watch.check() is False  # same stall: not reported again
    exporter.export(None)  # an export succeeds again
    assert watch.check() is False and watch.stalled is False
    clock.t = 400.0 + 181.0
    assert watch.check() is True  # a new stall is reported again


def test_on_stall_counts_and_dumps_every_thread() -> None:
    clock, exporter = _Clock(), _Exporter()
    watch = wd.ExportWatch(_Reader(exporter), clock=clock)
    before = wd.otel_metric_export_stalls_total._value.get()
    with tempfile.TemporaryFile(mode="w+") as stream:
        wd.on_stall(watch, stream=stream)
        stream.seek(0)
        dump = stream.read()
    assert wd.otel_metric_export_stalls_total._value.get() == before + 1
    assert "thread" in dump.lower() and "most recent call first" in dump


def test_finds_the_sdk_periodic_reader_and_wraps_its_exporter(monkeypatch) -> None:
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")  # conftest disables the SDK
    exporter = _Exporter()
    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=3_600_000)
    provider = MeterProvider(metric_readers=[reader])
    try:
        readers = wd.periodic_readers(provider)
        assert readers == [reader]
        watch = wd.ExportWatch(readers[0])
        assert watch.interval_s == 3600.0
        provider.get_meter("t").create_counter("c").add(1)
        reader.force_flush()  # collect + export through the wrapped exporter
        assert exporter.calls >= 1
    finally:
        provider.shutdown()


def test_watchdog_thread_starts_and_stops(monkeypatch) -> None:
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    exporter = _Exporter()
    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=3_600_000)
    provider = MeterProvider(metric_readers=[reader])
    stop = threading.Event()
    try:
        thread = wd.start_export_watchdog(provider, stop=stop, check_every_s=0.01)
        assert (
            thread is not None and thread.daemon and thread.name == "OtelExportWatchdog"
        )
        stop.set()
        thread.join(timeout=2)
        assert not thread.is_alive()
    finally:
        provider.shutdown()


def test_no_periodic_reader_starts_nothing() -> None:
    class _Provider:
        _metric_readers = ()

    assert wd.start_export_watchdog(_Provider()) is None


@pytest.mark.parametrize("bad", [object(), None])
def test_reader_without_exporter_is_refused(bad) -> None:
    class _NoExporter:
        _export_interval_millis = 60_000
        _exporter = bad

    if bad is None:
        with pytest.raises(ValueError):
            wd.ExportWatch(_NoExporter())
