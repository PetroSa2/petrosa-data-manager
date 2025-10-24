"""
Metrics collection middleware for detailed API monitoring.
"""

import time
from collections.abc import Callable

from fastapi import Request, Response
from prometheus_client import Counter, Gauge, Histogram
from starlette.middleware.base import BaseHTTPMiddleware

logger = __import__("logging").getLogger(__name__)

# Prometheus metrics
REQUEST_COUNT = Counter(
    "data_manager_requests_total",
    "Total number of API requests",
    ["method", "endpoint", "status_code"],
)

REQUEST_DURATION = Histogram(
    "data_manager_request_duration_seconds",
    "Request duration in seconds",
    ["method", "endpoint"],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)

REQUEST_SIZE = Histogram(
    "data_manager_request_size_bytes",
    "Request size in bytes",
    ["method", "endpoint"],
    buckets=[100, 1000, 10000, 100000, 1000000],
)

RESPONSE_SIZE = Histogram(
    "data_manager_response_size_bytes",
    "Response size in bytes",
    ["method", "endpoint"],
    buckets=[100, 1000, 10000, 100000, 1000000],
)

ERROR_RATE = Counter(
    "data_manager_errors_total",
    "Total number of API errors",
    ["method", "endpoint", "error_type"],
)

DATABASE_OPERATIONS = Counter(
    "data_manager_database_operations_total",
    "Total number of database operations",
    ["database", "operation", "status"],
)

DATABASE_OPERATION_DURATION = Histogram(
    "data_manager_database_operation_duration_seconds",
    "Database operation duration in seconds",
    ["database", "operation"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
)

CONNECTION_POOL_SIZE = Gauge(
    "data_manager_connection_pool_size",
    "Database connection pool size",
    ["database", "state"],  # state: active, idle, waiting
)

ACTIVE_CONNECTIONS = Gauge(
    "data_manager_active_connections",
    "Number of active database connections",
    ["database"],
)


class MetricsMiddleware(BaseHTTPMiddleware):
    """
    Middleware for collecting detailed API metrics.

    Tracks request counts, durations, sizes, error rates,
    and database operation metrics.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process request and collect metrics."""
        # Start timing
        start_time = time.time()

        # Extract endpoint info
        method = request.method
        endpoint = self._extract_endpoint(request)

        # Get request size
        request_size = await self._get_request_size(request)

        # Process request
        try:
            response = await call_next(request)
            status_code = response.status_code

            # Get response size
            response_size = self._get_response_size(response)

            # Record success metrics
            self._record_success_metrics(
                method, endpoint, status_code, start_time, request_size, response_size
            )

            return response

        except Exception as e:
            # Record error metrics
            self._record_error_metrics(method, endpoint, e, start_time, request_size)
            raise

    def _extract_endpoint(self, request: Request) -> str:
        """Extract endpoint name from request path."""
        path = request.url.path

        # Normalize endpoint names
        if path.startswith("/api/v1/"):
            # Generic API endpoints
            parts = path.split("/")
            if len(parts) >= 5:
                return f"/api/v1/{parts[3]}/{parts[4]}"
            elif len(parts) >= 4:
                return f"/api/v1/{parts[3]}"
        elif path.startswith("/data/"):
            # Domain-specific endpoints
            return path
        elif path.startswith("/health/"):
            # Health endpoints
            return path
        elif path.startswith("/raw/"):
            # Raw query endpoints
            return path

        return path

    async def _get_request_size(self, request: Request) -> int:
        """Get request body size."""
        try:
            if hasattr(request, "_body"):
                return len(request._body)
            elif hasattr(request, "body"):
                body = await request.body()
                return len(body)
            return 0
        except Exception:
            return 0

    def _get_response_size(self, response: Response) -> int:
        """Get response body size."""
        try:
            if hasattr(response, "body"):
                return len(response.body)
            return 0
        except Exception:
            return 0

    def _record_success_metrics(
        self,
        method: str,
        endpoint: str,
        status_code: int,
        start_time: float,
        request_size: int,
        response_size: int,
    ) -> None:
        """Record successful request metrics."""
        duration = time.time() - start_time

        # Record request metrics
        REQUEST_COUNT.labels(
            method=method, endpoint=endpoint, status_code=str(status_code)
        ).inc()
        REQUEST_DURATION.labels(method=method, endpoint=endpoint).observe(duration)
        REQUEST_SIZE.labels(method=method, endpoint=endpoint).observe(request_size)
        RESPONSE_SIZE.labels(method=method, endpoint=endpoint).observe(response_size)

        # Record error rate (0 for successful requests)
        if status_code >= 400:
            error_type = "client_error" if status_code < 500 else "server_error"
            ERROR_RATE.labels(
                method=method, endpoint=endpoint, error_type=error_type
            ).inc()

    def _record_error_metrics(
        self,
        method: str,
        endpoint: str,
        error: Exception,
        start_time: float,
        request_size: int,
    ) -> None:
        """Record error metrics."""
        duration = time.time() - start_time
        error_type = type(error).__name__

        # Record error metrics
        ERROR_RATE.labels(method=method, endpoint=endpoint, error_type=error_type).inc()
        REQUEST_DURATION.labels(method=method, endpoint=endpoint).observe(duration)
        REQUEST_SIZE.labels(method=method, endpoint=endpoint).observe(request_size)


def record_database_operation(
    database: str, operation: str, status: str, duration: float
) -> None:
    """Record database operation metrics."""
    DATABASE_OPERATIONS.labels(
        database=database, operation=operation, status=status
    ).inc()
    DATABASE_OPERATION_DURATION.labels(database=database, operation=operation).observe(
        duration
    )


def update_connection_pool_metrics(
    database: str, active: int, idle: int, waiting: int
) -> None:
    """Update connection pool metrics."""
    CONNECTION_POOL_SIZE.labels(database=database, state="active").set(active)
    CONNECTION_POOL_SIZE.labels(database=database, state="idle").set(idle)
    CONNECTION_POOL_SIZE.labels(database=database, state="waiting").set(waiting)


def update_active_connections(database: str, count: int) -> None:
    """Update active connections metric."""
    ACTIVE_CONNECTIONS.labels(database=database).set(count)
