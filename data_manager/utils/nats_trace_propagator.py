"""
NATS trace context propagation helper for OpenTelemetry.

This module provides utilities for injecting and extracting OpenTelemetry
trace context into/from NATS messages to enable distributed tracing across
service boundaries.
"""

import logging
from typing import Any

from opentelemetry import context, trace
from opentelemetry.propagate import extract, inject
from opentelemetry.trace import SpanKind
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

logger = logging.getLogger(__name__)

# Use W3C Trace Context propagator (standard for HTTP and message queues)
propagator = TraceContextTextMapPropagator()


class NATSTracePropagator:
    """Helper for injecting and extracting trace context in NATS messages."""

    # Field name for trace headers in message payload
    TRACE_HEADERS_FIELD = "_otel_trace_headers"

    @staticmethod
    def inject_context(message_dict: dict[str, Any]) -> dict[str, Any]:
        """
        Inject current trace context into message headers.

        This method extracts the current OpenTelemetry trace context and
        embeds it into the message dictionary under the _otel_trace_headers field.

        Args:
            message_dict: The message dictionary to inject context into

        Returns:
            The modified message dictionary with trace context injected

        Example:
            >>> data = {"symbol": "BTCUSDT", "price": 50000}
            >>> data_with_trace = NATSTracePropagator.inject_context(data)
            >>> print(data_with_trace.keys())
            dict_keys(['symbol', 'price', '_otel_trace_headers'])
        """
        try:
            # Create carrier for trace context
            carrier: dict[str, str] = {}

            # Inject current context into carrier
            inject(carrier)

            # Add trace headers to message (only if context was injected)
            if carrier:
                message_dict[NATSTracePropagator.TRACE_HEADERS_FIELD] = carrier
                logger.debug(
                    "Injected trace context into message",
                    extra={"trace_headers": carrier},
                )
            else:
                logger.debug("No active trace context to inject")

        except Exception as e:
            logger.warning(f"Failed to inject trace context: {e}")

        return message_dict

    @staticmethod
    def extract_context(message_dict: dict[str, Any]) -> context.Context | None:
        """
        Extract trace context from message and return as Context.

        This method extracts OpenTelemetry trace context from the message's
        _otel_trace_headers field and returns it as a Context object that
        can be used to create child spans.

        Args:
            message_dict: The message dictionary containing trace context

        Returns:
            Extracted Context object, or None if no context found

        Example:
            >>> data = {"_otel_trace_headers": {"traceparent": "..."}}
            >>> ctx = NATSTracePropagator.extract_context(data)
            >>> with tracer.start_as_current_span("process", context=ctx):
            ...     # Process message with trace context
        """
        try:
            # Get trace headers from message
            carrier = message_dict.get(NATSTracePropagator.TRACE_HEADERS_FIELD, {})

            if not carrier:
                logger.debug("No trace context found in message")
                return None

            # Extract context from carrier
            ctx = extract(carrier)

            logger.debug(
                "Extracted trace context from message",
                extra={"trace_headers": carrier},
            )

            return ctx

        except Exception as e:
            logger.warning(f"Failed to extract trace context: {e}")
            return None

    @staticmethod
    def create_span_from_message(
        tracer: trace.Tracer,
        message_dict: dict[str, Any],
        span_name: str,
        span_kind: SpanKind = SpanKind.CONSUMER,
        attributes: dict[str, Any] | None = None,
    ):
        """
        Create a span for message processing with extracted trace context.

        This is a convenience method that combines context extraction and
        span creation in one call. It extracts trace context from the message
        and creates a span as a child of that context.

        The span is automatically set as the current span and includes
        standard NATS messaging attributes.

        Args:
            tracer: OpenTelemetry tracer instance
            message_dict: The message dictionary containing trace context
            span_name: Name for the span
            span_kind: SpanKind (default: CONSUMER for message consumption)
            attributes: Additional span attributes to set

        Returns:
            Context manager for the span (use with 'with' statement)

        Example:
            >>> tracer = trace.get_tracer(__name__)
            >>> with NATSTracePropagator.create_span_from_message(
            ...     tracer, message_data, "process_market_data"
            ... ) as span:
            ...     # Process message
            ...     span.set_attribute("symbol", message_data["symbol"])
        """
        try:
            # Extract context from message
            ctx = NATSTracePropagator.extract_context(message_dict)

            # Prepare attributes with NATS-specific defaults
            span_attributes = {
                "messaging.system": "nats",
                "messaging.operation": "receive",
            }
            if attributes:
                span_attributes.update(attributes)

            # Create span as current span with extracted context
            return tracer.start_as_current_span(
                span_name,
                context=ctx,
                kind=span_kind,
                attributes=span_attributes,
            )

        except Exception as e:
            logger.warning(f"Failed to create span from message: {e}")
            # Return a context manager for a new span if extraction fails
            return tracer.start_as_current_span(
                span_name,
                kind=span_kind,
                attributes=attributes or {},
            )

    @staticmethod
    def remove_trace_headers(message_dict: dict[str, Any]) -> dict[str, Any]:
        """
        Remove trace headers from message dictionary.

        This can be useful to clean up messages before passing them to
        business logic that doesn't need the trace headers.

        Args:
            message_dict: The message dictionary

        Returns:
            The message dictionary without trace headers (modifies in-place)

        Example:
            >>> data = {"symbol": "BTCUSDT", "_otel_trace_headers": {...}}
            >>> cleaned = NATSTracePropagator.remove_trace_headers(data)
            >>> print("_otel_trace_headers" in cleaned)
            False
        """
        if NATSTracePropagator.TRACE_HEADERS_FIELD in message_dict:
            del message_dict[NATSTracePropagator.TRACE_HEADERS_FIELD]
        return message_dict
