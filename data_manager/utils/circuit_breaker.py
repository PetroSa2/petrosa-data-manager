"""
Circuit breaker implementation for database operations.

Based on petrosa-binance-data-extractor patterns.
"""

import logging
import time
from collections.abc import Callable
from enum import Enum

logger = logging.getLogger(__name__)


class CircuitBreakerState(Enum):
    """Circuit breaker states."""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Blocking all requests
    HALF_OPEN = "half_open"  # Testing if service recovered


class DatabaseCircuitBreaker:
    """
    Circuit breaker for database operations.

    Prevents cascading failures by blocking requests when error rate is high.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: int = 60,
        success_threshold: int = 2,
    ):
        """
        Initialize circuit breaker.

        Args:
            name: Identifier for this circuit breaker
            failure_threshold: Number of failures before opening circuit
            recovery_timeout: Seconds to wait before attempting recovery
            success_threshold: Number of successes needed to close circuit
        """
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.success_threshold = success_threshold

        self.state = CircuitBreakerState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time = None

    def call(self, func: Callable, *args, **kwargs):
        """
        Execute function with circuit breaker protection.

        Args:
            func: Function to execute
            *args: Positional arguments for function
            **kwargs: Keyword arguments for function

        Returns:
            Result of function execution

        Raises:
            Exception: If circuit is open or function fails
        """
        if self.state == CircuitBreakerState.OPEN:
            if self._should_attempt_reset():
                logger.info(
                    f"Circuit breaker {self.name}: Attempting reset to HALF_OPEN"
                )
                self.state = CircuitBreakerState.HALF_OPEN
            else:
                raise Exception(
                    f"Circuit breaker {self.name} is OPEN. "
                    f"Will retry after {self.recovery_timeout}s"
                )

        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except Exception as e:
            self._on_failure()
            raise e

    def _on_success(self):
        """Handle successful operation."""
        if self.state == CircuitBreakerState.HALF_OPEN:
            self.success_count += 1
            if self.success_count >= self.success_threshold:
                logger.info(
                    f"Circuit breaker {self.name}: "
                    f"Closing circuit after {self.success_count} successes"
                )
                self.state = CircuitBreakerState.CLOSED
                self.failure_count = 0
                self.success_count = 0
        else:
            self.failure_count = 0

    def _on_failure(self):
        """Handle failed operation."""
        self.failure_count += 1
        self.last_failure_time = time.time()

        if self.state == CircuitBreakerState.HALF_OPEN:
            logger.warning(
                f"Circuit breaker {self.name}: Reopening circuit after failure"
            )
            self.state = CircuitBreakerState.OPEN
            self.success_count = 0
        elif self.failure_count >= self.failure_threshold:
            logger.error(
                f"Circuit breaker {self.name}: Opening circuit after {self.failure_count} failures"
            )
            self.state = CircuitBreakerState.OPEN

    def _should_attempt_reset(self) -> bool:
        """Check if enough time has passed to attempt reset."""
        if self.last_failure_time is None:
            return True
        return (time.time() - self.last_failure_time) >= self.recovery_timeout

    def reset(self):
        """Manually reset circuit breaker to closed state."""
        logger.info(f"Circuit breaker {self.name}: Manual reset")
        self.state = CircuitBreakerState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time = None
