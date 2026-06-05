"""Bounded retry-with-backoff for transient database write failures.

Ported from petrosa-binance-data-extractor's error_classifier pattern. Used by
``MySQLAdapter.write`` to retry transient errors (lost connection, deadlocks,
gone-away server) before letting the call bubble through the circuit breaker
and count as one breaker failure.

Per petrosa-data-manager#213 F1: the retry helper is invoked INSIDE
``circuit_breaker.call`` — so each *exhausted* retry cycle counts as exactly
one breaker failure. With ``max_retries=3`` and breaker ``failure_threshold=5``
that means up to 5×(1+3)=20 underlying transient failures before the breaker
opens. Transient errors that resolve mid-cycle do not contribute at all.

Non-transient errors (``IntegrityError`` etc.) are re-raised immediately on the
first failure — no retry, full breaker accounting preserved.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TypeVar

try:
    from sqlalchemy.exc import IntegrityError, OperationalError
except ImportError:  # pragma: no cover - tested via adapter import path
    IntegrityError = None  # type: ignore[assignment, misc]
    OperationalError = None  # type: ignore[assignment, misc]

logger = logging.getLogger(__name__)

T = TypeVar("T")

# MySQL transient error codes (mysql_adapter operates on PyMySQL):
#   2003 - CR_CONN_HOST_ERROR (can't connect)
#   2006 - CR_SERVER_GONE_ERROR (server gone away)
#   2013 - CR_SERVER_LOST (connection lost mid-query)
#   1205 - ER_LOCK_WAIT_TIMEOUT
#   1213 - ER_LOCK_DEADLOCK
TRANSIENT_MYSQL_ERRNOS = frozenset({2003, 2006, 2013, 1205, 1213})


def is_transient(exc: BaseException) -> bool:
    """Return True iff exc is a known-transient database error worth retrying.

    Non-transient by definition: IntegrityError (duplicate-key / FK / NOT NULL
    violations), value errors, type errors, generic DatabaseError without an
    identifiable transient code.
    """
    if IntegrityError is not None and isinstance(exc, IntegrityError):
        return False
    if OperationalError is not None and isinstance(exc, OperationalError):
        code = _extract_mysql_errno(exc)
        if code is None:
            return True
        return code in TRANSIENT_MYSQL_ERRNOS
    return False


def _extract_mysql_errno(exc: BaseException) -> int | None:
    """Best-effort dig the numeric MySQL errno out of a SQLAlchemy/PyMySQL exception."""
    orig = getattr(exc, "orig", None)
    args = getattr(orig, "args", None) if orig is not None else None
    if args and isinstance(args, tuple) and args:
        first = args[0]
        if isinstance(first, int):
            return first
    return None


def retry_transient(
    func: Callable[[], T],
    *,
    max_retries: int = 3,
    backoff_base: float = 0.2,
    backoff_cap: float = 5.0,
    sleeper: Callable[[float], None] = time.sleep,
) -> T:
    """Call ``func`` and retry on transient errors with exponential backoff.

    Raises the last exception if all attempts are exhausted, or immediately if
    the first error is non-transient. ``sleeper`` is injectable so tests run
    without real wall-clock waits.
    """
    last_exc: BaseException | None = None
    for attempt in range(1, max_retries + 2):
        try:
            return func()
        except Exception as exc:
            last_exc = exc
            if not is_transient(exc):
                raise
            if attempt > max_retries:
                logger.warning(
                    "retry_transient exhausted after %d attempts: %s",
                    attempt,
                    exc,
                )
                raise
            delay = min(backoff_base * (2 ** (attempt - 1)), backoff_cap)
            logger.info(
                "retry_transient attempt %d/%d failed (%s); sleeping %.2fs",
                attempt,
                max_retries,
                exc,
                delay,
            )
            sleeper(delay)
    assert last_exc is not None  # pragma: no cover
    raise last_exc
