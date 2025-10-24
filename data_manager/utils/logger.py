"""
Structured logging setup for the Petrosa Data Manager service.

This module provides structured logging configuration using structlog
with JSON formatting and proper correlation IDs.
"""

import logging
import sys

import structlog
from structlog.stdlib import LoggerFactory


def setup_logging(
    level: str = "INFO", format_type: str = "json"
) -> structlog.BoundLogger:
    """
    Set up structured logging for the service.

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        format_type: Log format type ("json" or "text")

    Returns:
        Configured structlog logger
    """
    # Configure standard library logging
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper()),
    )

    # Configure structlog
    if format_type.lower() == "json":
        structlog.configure(
            processors=[
                structlog.stdlib.filter_by_level,
                structlog.stdlib.add_logger_name,
                structlog.stdlib.add_log_level,
                structlog.stdlib.PositionalArgumentsFormatter(),
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
                structlog.processors.UnicodeDecoder(),
                structlog.processors.JSONRenderer(),
            ],
            context_class=dict,
            logger_factory=LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )
    else:
        structlog.configure(
            processors=[
                structlog.stdlib.filter_by_level,
                structlog.stdlib.add_logger_name,
                structlog.stdlib.add_log_level,
                structlog.stdlib.PositionalArgumentsFormatter(),
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
                structlog.processors.UnicodeDecoder(),
                structlog.dev.ConsoleRenderer(),
            ],
            context_class=dict,
            logger_factory=LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )

    # Create logger with service context
    logger = structlog.get_logger("data-manager")

    # Add service metadata
    import os

    logger = logger.bind(
        service_name="data-manager",
        service_version=os.getenv("OTEL_SERVICE_VERSION", "1.0.0"),
        environment=os.getenv("ENVIRONMENT", "production"),
    )

    return logger


def get_logger(name: str | None = None) -> structlog.BoundLogger:
    """
    Get a logger instance.

    Args:
        name: Logger name (optional)

    Returns:
        Configured structlog logger
    """
    if name:
        return structlog.get_logger(name)
    else:
        return structlog.get_logger("data-manager")


def add_correlation_id(
    logger: structlog.BoundLogger, correlation_id: str
) -> structlog.BoundLogger:
    """
    Add correlation ID to logger context.

    Args:
        logger: Logger instance
        correlation_id: Correlation ID for request tracing

    Returns:
        Logger with correlation ID bound
    """
    return logger.bind(correlation_id=correlation_id)


def add_request_context(
    logger: structlog.BoundLogger, **kwargs
) -> structlog.BoundLogger:
    """
    Add request context to logger.

    Args:
        logger: Logger instance
        **kwargs: Context key-value pairs

    Returns:
        Logger with context bound
    """
    return logger.bind(**kwargs)
