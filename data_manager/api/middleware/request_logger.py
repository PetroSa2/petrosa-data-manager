"""
Request/response logging middleware for comprehensive API monitoring.
"""

import logging
import time
import uuid
from collections.abc import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

import constants

logger = logging.getLogger(__name__)


class RequestLoggerMiddleware(BaseHTTPMiddleware):
    """
    Middleware for comprehensive request/response logging.

    Logs every request with method, path, query params, body size,
    response status, response time, and response size.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process request and response with detailed logging."""
        # Generate request ID for tracing
        request_id = str(uuid.uuid4())

        # Start timing
        start_time = time.time()

        # Log request details
        await self._log_request(request, request_id)

        # Process request
        try:
            response = await call_next(request)
        except Exception as e:
            # Log error
            await self._log_error(request, request_id, e, start_time)
            raise

        # Calculate processing time
        process_time = time.time() - start_time

        # Log response details
        await self._log_response(request, response, request_id, process_time)

        return response

    async def _log_request(self, request: Request, request_id: str) -> None:
        """Log incoming request details."""
        if not constants.LOG_REQUEST_DETAILS:
            return

        try:
            # Get request body size
            body_size = 0
            if hasattr(request, "_body"):
                body_size = len(request._body)
            elif hasattr(request, "body"):
                body = await request.body()
                body_size = len(body)

            # Build request log
            request_log = {
                "type": "request",
                "request_id": request_id,
                "method": request.method,
                "url": str(request.url),
                "path": request.url.path,
                "query_params": dict(request.query_params),
                "headers": dict(request.headers),
                "body_size": body_size,
                "client_ip": request.client.host if request.client else None,
                "timestamp": time.time(),
            }

            # Log query details if enabled
            if constants.LOG_QUERY_DETAILS and "query" in request.url.path:
                request_log["query_details"] = {
                    "database": request.path_params.get("database"),
                    "collection": request.path_params.get("collection"),
                }

            logger.debug("API Request", extra=request_log)

        except Exception as e:
            logger.error(f"Error logging request {request_id}: {e}")

    async def _log_response(
        self, request: Request, response: Response, request_id: str, process_time: float
    ) -> None:
        """Log response details."""
        if not constants.LOG_RESPONSE_DETAILS:
            return

        try:
            # Get response body size
            response_size = 0
            if hasattr(response, "body"):
                response_size = len(response.body)

            # Build response log
            response_log = {
                "type": "response",
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "response_size": response_size,
                "process_time_ms": round(process_time * 1000, 2),
                "timestamp": time.time(),
            }

            # Add performance indicators
            if process_time > 1.0:  # Slow request
                response_log["performance"] = "slow"
            elif process_time > 0.5:  # Medium request
                response_log["performance"] = "medium"
            else:
                response_log["performance"] = "fast"

            # Add error indicators
            if response.status_code >= 400:
                response_log["error"] = True
                response_log["error_type"] = (
                    "client_error" if response.status_code < 500 else "server_error"
                )

            logger.debug("API Response", extra=response_log)

        except Exception as e:
            logger.error(f"Error logging response {request_id}: {e}")

    async def _log_error(
        self, request: Request, request_id: str, error: Exception, start_time: float
    ) -> None:
        """Log error details."""
        try:
            process_time = time.time() - start_time

            error_log = {
                "type": "error",
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "error_type": type(error).__name__,
                "error_message": str(error),
                "process_time_ms": round(process_time * 1000, 2),
                "timestamp": time.time(),
            }

            logger.error("API Error", extra=error_log, exc_info=True)

        except Exception as e:
            logger.error(f"Error logging error {request_id}: {e}")
