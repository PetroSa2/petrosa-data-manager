"""
Unit tests for data_manager.utils.* modules (logger, time_utils, circuit_breaker)
that previously had low or zero coverage.
"""

import logging as stdlib_logging
import time
from datetime import UTC, datetime, timedelta, timezone

import pytest

from data_manager.utils.circuit_breaker import (
    CircuitBreakerState,
    DatabaseCircuitBreaker,
)
from data_manager.utils.logger import (
    add_correlation_id,
    add_request_context,
    get_logger,
    setup_logging,
)
from data_manager.utils.time_utils import (
    calculate_expected_records,
    create_time_chunks,
    parse_timeframe_to_minutes,
    parse_timeframe_to_seconds,
)


class TestSetupLogging:
    def test_json_format_returns_bound_logger(self):
        log = setup_logging(level="INFO", format_type="json")
        assert log is not None
        assert hasattr(log, "info")
        assert hasattr(log, "bind")

    def test_text_format_returns_bound_logger(self):
        log = setup_logging(level="DEBUG", format_type="text")
        assert log is not None

    def test_uses_specified_level(self):
        setup_logging(level="WARNING", format_type="json")
        # The root logger's level should be set to WARNING (30).
        assert stdlib_logging.getLogger().level == stdlib_logging.WARNING

    def test_can_rebind_context(self):
        log = setup_logging(level="INFO", format_type="json")
        rebound = log.bind(custom="value")
        assert rebound is not None


class TestGetLogger:
    def test_returns_named_logger(self):
        log = get_logger("custom.module")
        assert log is not None

    def test_returns_service_logger_by_default(self):
        log = get_logger()
        assert log is not None


class TestAddCorrelationId:
    def test_returns_logger_with_correlation_id(self):
        log = get_logger()
        bound = add_correlation_id(log, "abc-123")
        assert bound is not None


class TestAddRequestContext:
    def test_returns_logger_with_context(self):
        log = get_logger()
        bound = add_request_context(log, request_id="r-1", user_id="u-2")
        assert bound is not None

    def test_empty_kwargs_returns_logger(self):
        log = get_logger()
        bound = add_request_context(log)
        assert bound is not None


class TestParseTimeframeToMinutes:
    @pytest.mark.parametrize(
        "tf,expected",
        [
            ("1m", 1),
            ("5m", 5),
            ("15m", 15),
            ("1h", 60),
            ("4h", 240),
            ("1d", 1440),
            ("1w", 10080),
            ("3w", 30240),
        ],
    )
    def test_known_timeframes(self, tf, expected):
        assert parse_timeframe_to_minutes(tf) == expected

    def test_case_insensitive(self):
        assert parse_timeframe_to_minutes("1H") == 60
        assert parse_timeframe_to_minutes("1D") == 1440

    def test_invalid_unit_raises(self):
        with pytest.raises(ValueError, match="Invalid timeframe") as exc_info:
            parse_timeframe_to_minutes("5x")
        assert "5x" in str(exc_info.value)

    def test_invalid_number_propagates(self):
        with pytest.raises(ValueError) as exc_info:
            parse_timeframe_to_minutes("abch")
        assert exc_info.value is not None


class TestParseTimeframeToSeconds:
    @pytest.mark.parametrize(
        "tf,expected_seconds",
        [
            ("1m", 60),
            ("5m", 300),
            ("1h", 3600),
            ("1d", 86400),
        ],
    )
    def test_known_timeframes(self, tf, expected_seconds):
        assert parse_timeframe_to_seconds(tf) == expected_seconds


class TestCalculateExpectedRecords:
    def test_one_hour_at_one_minute_interval(self):
        start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        end = start + timedelta(hours=1)
        assert calculate_expected_records(start, end, "1m") == 60

    def test_one_day_at_one_hour_interval(self):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = start + timedelta(days=1)
        assert calculate_expected_records(start, end, "1h") == 24

    def test_partial_interval_truncates(self):
        # 90 seconds / 60s interval = 1.5 → int truncates to 1.
        start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        end = datetime(2026, 1, 1, 12, 1, 30, tzinfo=UTC)
        assert calculate_expected_records(start, end, "1m") == 1


class TestCreateTimeChunks:
    def test_single_chunk_when_range_fits(self):
        start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        end = start + timedelta(minutes=30)
        chunks = create_time_chunks(start, end, chunk_size_minutes=60)
        assert len(chunks) == 1
        assert chunks[0] == (start, end)

    def test_multiple_chunks(self):
        start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        end = start + timedelta(hours=3)
        chunks = create_time_chunks(start, end, chunk_size_minutes=60)
        assert len(chunks) == 3
        # Each chunk is 60 minutes
        for s, e in chunks:
            assert (e - s) <= timedelta(minutes=60)

    def test_final_chunk_is_truncated_to_end(self):
        start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        end = start + timedelta(minutes=90)
        chunks = create_time_chunks(start, end, chunk_size_minutes=60)
        assert len(chunks) == 2
        # Final chunk ends at `end` (not chunk_size_minutes past start of chunk).
        assert chunks[-1][1] == end

    def test_empty_range_returns_empty_list(self):
        start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        chunks = create_time_chunks(start, start, chunk_size_minutes=60)
        assert chunks == []


class TestCircuitBreakerClosed:
    def test_initial_state_is_closed(self):
        cb = DatabaseCircuitBreaker(name="db")
        assert cb.state == CircuitBreakerState.CLOSED
        assert cb.failure_count == 0
        assert cb.success_count == 0
        assert cb.last_failure_time is None

    def test_successful_call_passes_result_through(self):
        cb = DatabaseCircuitBreaker(name="db")
        assert cb.call(lambda x: x + 1, 41) == 42
        assert cb.state == CircuitBreakerState.CLOSED

    def test_failure_increments_counter(self):
        cb = DatabaseCircuitBreaker(name="db", failure_threshold=3)

        def boom():
            raise RuntimeError("x")

        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb.call(boom)
        assert cb.failure_count == 2
        assert cb.state == CircuitBreakerState.CLOSED


class TestCircuitBreakerOpens:
    def test_opens_after_reaching_failure_threshold(self):
        cb = DatabaseCircuitBreaker(name="db", failure_threshold=3)

        def boom():
            raise RuntimeError("x")

        for _ in range(3):
            with pytest.raises(RuntimeError):
                cb.call(boom)
        assert cb.state == CircuitBreakerState.OPEN
        assert cb.failure_count == 3

    def test_open_circuit_rejects_calls(self):
        cb = DatabaseCircuitBreaker(name="db", failure_threshold=1, recovery_timeout=60)

        def boom():
            raise RuntimeError("x")

        with pytest.raises(RuntimeError):
            cb.call(boom)
        # Now state is OPEN, next call must be rejected without invoking func.
        with pytest.raises(Exception, match="OPEN") as exc_info:
            cb.call(lambda: "should not run")
        assert "OPEN" in str(exc_info.value)


class TestCircuitBreakerRecovery:
    def test_attempts_half_open_after_timeout(self):
        cb = DatabaseCircuitBreaker(name="db", failure_threshold=1, recovery_timeout=0)

        def boom():
            raise RuntimeError("x")

        with pytest.raises(RuntimeError):
            cb.call(boom)
        # State is OPEN; with recovery_timeout=0, the next call should attempt reset.
        assert cb.state == CircuitBreakerState.OPEN

        # Successful call from HALF_OPEN should keep the cb partway to CLOSED.
        result = cb.call(lambda: "ok")
        assert result == "ok"
        # success_threshold defaults to 2 — one success leaves us in HALF_OPEN.
        assert cb.state == CircuitBreakerState.HALF_OPEN

    def test_closes_after_enough_successes_in_half_open(self):
        cb = DatabaseCircuitBreaker(
            name="db",
            failure_threshold=1,
            recovery_timeout=0,
            success_threshold=2,
        )

        def boom():
            raise RuntimeError("x")

        with pytest.raises(RuntimeError):
            cb.call(boom)
        # Two successes in HALF_OPEN must close the circuit.
        cb.call(lambda: "ok")
        cb.call(lambda: "ok")
        assert cb.state == CircuitBreakerState.CLOSED
        assert cb.failure_count == 0
        assert cb.success_count == 0

    def test_reopens_on_failure_during_half_open(self):
        cb = DatabaseCircuitBreaker(name="db", failure_threshold=1, recovery_timeout=0)

        with pytest.raises(RuntimeError):
            cb.call(lambda: (_ for _ in ()).throw(RuntimeError("x")))
        # Now in OPEN with recovery_timeout=0 → next call goes to HALF_OPEN
        # Failure during HALF_OPEN must reopen.
        with pytest.raises(RuntimeError):
            cb.call(lambda: (_ for _ in ()).throw(RuntimeError("y")))
        assert cb.state == CircuitBreakerState.OPEN


class TestCircuitBreakerReset:
    def test_manual_reset_clears_state(self):
        cb = DatabaseCircuitBreaker(name="db", failure_threshold=1)

        def boom():
            raise RuntimeError("x")

        with pytest.raises(RuntimeError):
            cb.call(boom)
        assert cb.state == CircuitBreakerState.OPEN

        cb.reset()
        assert cb.state == CircuitBreakerState.CLOSED
        assert cb.failure_count == 0
        assert cb.success_count == 0
        assert cb.last_failure_time is None


class TestCircuitBreakerShouldAttemptReset:
    def test_returns_true_when_no_prior_failure(self):
        cb = DatabaseCircuitBreaker(name="db")
        # last_failure_time is None on a fresh instance.
        assert cb._should_attempt_reset() is True

    def test_returns_false_during_recovery_window(self):
        cb = DatabaseCircuitBreaker(name="db", recovery_timeout=60)
        cb.last_failure_time = time.time()
        assert cb._should_attempt_reset() is False

    def test_returns_true_after_recovery_window(self):
        cb = DatabaseCircuitBreaker(name="db", recovery_timeout=0)
        cb.last_failure_time = time.time() - 1.0
        assert cb._should_attempt_reset() is True
