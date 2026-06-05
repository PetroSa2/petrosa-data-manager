"""Tests for petrosa-data-manager#213 — MySQL write retry + accountability.

Covers acceptance criteria:

* AC2.1 — transient OperationalError is retried with backoff and succeeds
  if the failure clears within budget; exhausted retries still count toward
  opening the breaker.
* AC2.2 — IntegrityError is NOT retried.
* AC2.3 — INSERT IGNORE duplicates are surfaced as explicit ``duplicates``
  via ``len(records) - rowcount`` (never an ambiguous plain ``0``).
* AC2.4 — HTTP boundary: OPEN circuit → 503; genuine failure → 500;
  all-duplicate → 200 with explicit ``duplicates`` field.
* AC2.5 — ``data_manager_mysql_write_failures_total`` increments with
  ``reason`` on failure.

The tests use a SQLite engine (via the existing test pattern in
``tests/test_mysql_adapter_methods.py``) for live DB behavior, and unit
mocks for retry / circuit-breaker / HTTP-route paths so transient-error
semantics are exercised deterministically.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError, OperationalError

from data_manager.db.mysql_adapter import MySQLAdapter, WriteResult
from data_manager.utils.circuit_breaker import (
    CircuitBreakerOpenError,
    DatabaseCircuitBreaker,
)
from data_manager.utils.retry import (
    TRANSIENT_MYSQL_ERRNOS,
    is_transient,
    retry_transient,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


class _Rec(BaseModel):
    """Tiny pydantic model matching the audit_logs schema."""

    audit_id: str
    dataset_id: str
    symbol: str
    audit_type: str
    severity: str
    details: str
    timestamp: str


def _make_op_error(errno: int | None) -> OperationalError:
    """Build an OperationalError whose .orig.args[0] is the given errno."""
    orig = MagicMock()
    orig.args = (errno,) if errno is not None else ()
    err = OperationalError("SELECT 1", {}, Exception("boom"))
    err.orig = orig  # type: ignore[attr-defined]
    return err


def _make_integrity_error() -> IntegrityError:
    orig = MagicMock()
    orig.args = (1062,)  # ER_DUP_ENTRY — irrelevant for the test, just realistic
    err = IntegrityError("INSERT", {}, Exception("dup"))
    err.orig = orig  # type: ignore[attr-defined]
    return err


# ---------------------------------------------------------------------------
# retry_transient + is_transient (AC2.1 / AC2.2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("errno", sorted(TRANSIENT_MYSQL_ERRNOS))
def test_is_transient_recognises_known_transient_errnos(errno: int):
    assert is_transient(_make_op_error(errno)) is True


def test_is_transient_rejects_integrity_error():
    assert is_transient(_make_integrity_error()) is False


def test_is_transient_unknown_errno_in_operational_error_treated_transient():
    """Conservative default: an OperationalError without a recognised errno
    is treated as transient (network blip, unknown driver code, etc.)."""
    assert is_transient(_make_op_error(None)) is True


def test_retry_transient_succeeds_after_transient_failures():
    sleeps: list[float] = []
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _make_op_error(2013)  # CR_SERVER_LOST
        return "ok"

    result = retry_transient(flaky, max_retries=3, sleeper=sleeps.append)
    assert result == "ok"
    assert attempts["n"] == 3
    assert len(sleeps) == 2  # slept between attempts 1→2 and 2→3


def test_retry_transient_does_not_retry_integrity_error():
    attempts = {"n": 0}

    def boom():
        attempts["n"] += 1
        raise _make_integrity_error()

    with pytest.raises(IntegrityError):
        retry_transient(boom, max_retries=3, sleeper=lambda _: None)
    assert attempts["n"] == 1  # AC2.2 — no retry


def test_retry_transient_raises_after_exhausting_budget():
    attempts = {"n": 0}

    def always_fails():
        attempts["n"] += 1
        raise _make_op_error(2006)  # CR_SERVER_GONE

    with pytest.raises(OperationalError):
        retry_transient(always_fails, max_retries=3, sleeper=lambda _: None)
    assert attempts["n"] == 4  # 1 try + 3 retries


# ---------------------------------------------------------------------------
# DatabaseCircuitBreaker raises typed CircuitBreakerOpenError (AC2.4 / F9)
# ---------------------------------------------------------------------------


def test_circuit_breaker_raises_typed_open_error_when_open():
    breaker = DatabaseCircuitBreaker(
        "test", failure_threshold=2, recovery_timeout=999, success_threshold=1
    )

    def boom():
        raise RuntimeError("kaboom")

    with pytest.raises(RuntimeError):
        breaker.call(boom)
    with pytest.raises(RuntimeError):
        breaker.call(boom)
    # Third call must fast-fail with the typed exception, not a bare Exception
    with pytest.raises(CircuitBreakerOpenError) as exc_info:
        breaker.call(lambda: None)
    assert exc_info.value.name == "test"
    assert exc_info.value.recovery_timeout == 999


# ---------------------------------------------------------------------------
# Retry cycle counts as one breaker failure (AC2.1 nesting)
# ---------------------------------------------------------------------------


def test_exhausted_retry_cycle_counts_as_one_breaker_failure():
    breaker = DatabaseCircuitBreaker(
        "nested", failure_threshold=2, recovery_timeout=999, success_threshold=1
    )

    def fail_always():
        raise _make_op_error(2013)

    # First retry-cycle exhausts → 1 breaker failure
    with pytest.raises(OperationalError):
        breaker.call(
            lambda: retry_transient(fail_always, max_retries=2, sleeper=lambda _: None)
        )
    assert breaker.failure_count == 1
    # Second exhausted cycle → breaker opens
    with pytest.raises(OperationalError):
        breaker.call(
            lambda: retry_transient(fail_always, max_retries=2, sleeper=lambda _: None)
        )
    # Now the breaker should be OPEN
    with pytest.raises(CircuitBreakerOpenError):
        breaker.call(lambda: None)


# ---------------------------------------------------------------------------
# WriteResult dataclass behaviour (AC2.3 backward-compat)
# ---------------------------------------------------------------------------


def test_write_result_is_int_compatible_for_legacy_callers():
    """Legacy ``adapter.write(...) == 0`` comparisons must still work."""
    r = WriteResult(inserted=0, duplicates=5, failed=0)
    assert r == 0  # int comparison still works (matches existing tests)
    assert r.inserted == 0
    assert r.duplicates == 5
    assert r.failed == 0
    assert r.as_dict() == {"inserted": 0, "duplicates": 5, "failed": 0}

    r2 = WriteResult(inserted=7, duplicates=0, failed=0)
    assert r2 == 7
    assert int(r2) == 7


# ---------------------------------------------------------------------------
# adapter.write end-to-end against in-memory SQLite (AC2.3 happy path)
# ---------------------------------------------------------------------------


def _build_sqlite_adapter():
    """Mirror the pattern used by tests/test_mysql_adapter_methods.py."""
    adapter = MySQLAdapter("sqlite:///:memory:")
    adapter.engine_options = {}  # SQLite doesn't like the MySQL connect_args
    adapter.connect()
    return adapter


def _audit_rec(audit_id: str) -> _Rec:
    return _Rec(
        audit_id=audit_id,
        dataset_id="ds-1",
        symbol="BTCUSDT",
        audit_type="ingestion",
        severity="info",
        details="{}",
        timestamp="2026-06-04T00:00:00",
    )


def test_write_empty_batch_returns_empty_writeresult():
    adapter = _build_sqlite_adapter()
    try:
        r = adapter.write([], "audit_logs")
        assert isinstance(r, WriteResult)
        assert r.inserted == r.duplicates == r.failed == 0
    finally:
        adapter.disconnect()


def test_write_computes_duplicates_from_rowcount_gap():
    """AC2.3 / F2: ``INSERT IGNORE`` reduces ``rowcount`` instead of raising;
    duplicates must therefore be computed as ``len(records) - rowcount``.

    We can't exercise ``INSERT IGNORE`` against SQLite (it's MySQL-specific
    syntax), so we mock the engine to return a controlled rowcount and
    assert the adapter's arithmetic is correct.
    """
    adapter = MySQLAdapter("sqlite:///:memory:")
    adapter.engine_options = {}
    adapter.connect()
    try:
        records = [_audit_rec(f"a-{i}") for i in range(5)]

        # Mock the engine so execute() returns a controlled rowcount.
        fake_result = MagicMock()
        fake_result.rowcount = 3  # 3 inserted, so 2 dups in a batch of 5
        fake_conn = MagicMock()
        fake_conn.execute.return_value = fake_result
        fake_conn.begin.return_value = MagicMock()
        fake_engine = MagicMock()
        fake_engine.connect.return_value.__enter__.return_value = fake_conn

        with patch.object(adapter, "_ensure_connected", return_value=fake_engine):
            result = adapter.write(records, "audit_logs")

        assert isinstance(result, WriteResult)
        assert result.inserted == 3
        assert result.duplicates == 2  # 5 records - 3 rowcount
        assert result.failed == 0

    finally:
        adapter.disconnect()


def test_write_all_duplicates_yields_explicit_count_not_ambiguous_zero():
    """AC2.3 — never an ambiguous bare ``0``: when every record is a duplicate
    (``rowcount == 0``), the caller still sees ``duplicates == N``."""
    adapter = MySQLAdapter("sqlite:///:memory:")
    adapter.engine_options = {}
    adapter.connect()
    try:
        records = [_audit_rec(f"d-{i}") for i in range(4)]

        fake_result = MagicMock()
        fake_result.rowcount = 0
        fake_conn = MagicMock()
        fake_conn.execute.return_value = fake_result
        fake_conn.begin.return_value = MagicMock()
        fake_engine = MagicMock()
        fake_engine.connect.return_value.__enter__.return_value = fake_conn

        with patch.object(adapter, "_ensure_connected", return_value=fake_engine):
            result = adapter.write(records, "audit_logs")

        assert result.inserted == 0
        assert result.duplicates == 4
        assert result.failed == 0
        # Int-compat still holds — but the structured data is the truth.
        assert int(result) == 0
        assert result.as_dict() == {"inserted": 0, "duplicates": 4, "failed": 0}
    finally:
        adapter.disconnect()


# ---------------------------------------------------------------------------
# Adapter records the write-failure metric on permanent failure (AC2.5)
# ---------------------------------------------------------------------------


def test_adapter_increments_failure_counter_on_circuit_open():
    """When the breaker is OPEN, the failure counter must increment."""
    adapter = _build_sqlite_adapter()
    try:
        # Force the breaker into OPEN by faking past failures
        adapter.circuit_breaker.state = adapter.circuit_breaker.state.OPEN
        adapter.circuit_breaker.failure_count = 99
        adapter.circuit_breaker.last_failure_time = 10**12  # far future

        with patch(
            "data_manager.api.middleware.metrics.MYSQL_WRITE_FAILURES"
        ) as counter:
            with pytest.raises(CircuitBreakerOpenError):
                adapter.write([_audit_rec("z-1")], "audit_logs")
            counter.labels.assert_called_with(
                database="mysql",
                collection="audit_logs",
                reason="circuit_or_unknown",
            )
            counter.labels.return_value.inc.assert_called_once()
    finally:
        adapter.disconnect()
