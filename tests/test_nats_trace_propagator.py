"""
Tests for NATS trace context propagation.
"""

import os
import unittest
from unittest.mock import patch

# Set OTEL_NO_AUTO_INIT before importing any modules that might initialize OpenTelemetry
os.environ["OTEL_NO_AUTO_INIT"] = "1"

from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

from data_manager.utils.nats_trace_propagator import NATSTracePropagator  # noqa: E402


class TestNATSTracePropagator(unittest.TestCase):
    """Test cases for NATSTracePropagator."""

    def setUp(self):
        """Set up test fixtures."""
        # Create in-memory exporter and tracer provider for testing
        self.exporter = InMemorySpanExporter()
        self.tracer_provider = TracerProvider()
        self.tracer_provider.add_span_processor(SimpleSpanProcessor(self.exporter))

        # Only set tracer provider if not already set
        current_provider = trace.get_tracer_provider()
        if not hasattr(current_provider, "_atexit_handler"):
            trace.set_tracer_provider(self.tracer_provider)
        else:
            # Use existing global provider for tests
            self.tracer_provider = current_provider

        self.tracer = trace.get_tracer(__name__)

    def tearDown(self):
        """Clean up after tests."""
        if hasattr(self.exporter, "clear"):
            self.exporter.clear()

    def test_inject_context_with_active_span(self):
        """Test injecting trace context when there's an active span."""
        message_dict = {"symbol": "BTCUSDT", "price": 50000}

        # Create a span to establish active context
        with self.tracer.start_as_current_span("test_span"):
            result = NATSTracePropagator.inject_context(message_dict)

            # Verify trace headers were added
            self.assertIn(NATSTracePropagator.TRACE_HEADERS_FIELD, result)
            trace_headers = result[NATSTracePropagator.TRACE_HEADERS_FIELD]

            # Should have traceparent header (W3C Trace Context format)
            self.assertIn("traceparent", trace_headers)
            self.assertIsInstance(trace_headers["traceparent"], str)

            # Original data should still be present
            self.assertEqual(result["symbol"], "BTCUSDT")
            self.assertEqual(result["price"], 50000)

    def test_inject_context_without_active_span(self):
        """Test injecting trace context when there's no active span."""
        message_dict = {"symbol": "ETHUSDT", "price": 3000}

        result = NATSTracePropagator.inject_context(message_dict)

        # Without active span, no trace headers should be added
        # (or empty headers depending on OpenTelemetry behavior)
        # Original data should still be present
        self.assertEqual(result["symbol"], "ETHUSDT")
        self.assertEqual(result["price"], 3000)

    def test_extract_context_with_trace_headers(self):
        """Test extracting trace context from message with headers."""
        # First inject context to get valid trace headers
        with self.tracer.start_as_current_span("test_span"):
            message_dict = {"symbol": "BTCUSDT"}
            message_with_trace = NATSTracePropagator.inject_context(message_dict)

            # Now extract from the message
            ctx = NATSTracePropagator.extract_context(message_with_trace)

            # Should successfully extract context
            self.assertIsNotNone(ctx)

    def test_extract_context_without_trace_headers(self):
        """Test extracting trace context from message without headers."""
        message_dict = {"symbol": "BTCUSDT", "price": 50000}

        ctx = NATSTracePropagator.extract_context(message_dict)

        # Should return None when no trace headers present
        self.assertIsNone(ctx)

    def test_extract_context_with_empty_headers(self):
        """Test extracting trace context from message with empty headers."""
        message_dict = {
            "symbol": "BTCUSDT",
            NATSTracePropagator.TRACE_HEADERS_FIELD: {},
        }

        ctx = NATSTracePropagator.extract_context(message_dict)

        # Should return None when headers are empty
        self.assertIsNone(ctx)

    def test_create_span_from_message_with_context(self):
        """Test creating span from message with trace context."""
        # Create parent span and inject context
        with self.tracer.start_as_current_span("parent_span") as parent:
            parent_span_context = parent.get_span_context()
            message_dict = {"symbol": "BTCUSDT", "event_type": "trade"}
            message_with_trace = NATSTracePropagator.inject_context(message_dict)

        # Clear any active spans
        self.exporter.clear()

        # Now create child span from the message
        with NATSTracePropagator.create_span_from_message(
            self.tracer,
            message_with_trace,
            "child_span",
            attributes={"test_attr": "test_value"},
        ) as child:
            child_span_context = child.get_span_context()

            # Verify it's a valid span
            self.assertTrue(child_span_context.is_valid)

        # Verify span was created
        spans = self.exporter.get_finished_spans()
        self.assertEqual(len(spans), 1)

        # Verify span attributes
        span = spans[0]
        self.assertEqual(span.name, "child_span")
        self.assertEqual(span.kind, trace.SpanKind.CONSUMER)
        self.assertIn("test_attr", span.attributes)
        self.assertEqual(span.attributes["test_attr"], "test_value")
        self.assertEqual(span.attributes["messaging.system"], "nats")
        self.assertEqual(span.attributes["messaging.operation"], "receive")

        # Verify trace ID matches parent
        self.assertEqual(span.context.trace_id, parent_span_context.trace_id)

    def test_create_span_from_message_without_context(self):
        """Test creating span from message without trace context."""
        message_dict = {"symbol": "ETHUSDT", "event_type": "ticker"}

        with NATSTracePropagator.create_span_from_message(
            self.tracer, message_dict, "orphan_span"
        ) as span:
            # Should still create a valid span (as new trace root)
            self.assertTrue(span.get_span_context().is_valid)
            self.assertEqual(span.name, "orphan_span")
            # Verify NATS attributes were set
            self.assertIn("messaging.system", span.attributes)
            self.assertEqual(span.attributes["messaging.system"], "nats")

    def test_remove_trace_headers(self):
        """Test removing trace headers from message."""
        message_dict = {
            "symbol": "BTCUSDT",
            "price": 50000,
            NATSTracePropagator.TRACE_HEADERS_FIELD: {
                "traceparent": "00-1234567890abcdef-0123456789abcdef-01"
            },
        }

        result = NATSTracePropagator.remove_trace_headers(message_dict)

        # Trace headers should be removed
        self.assertNotIn(NATSTracePropagator.TRACE_HEADERS_FIELD, result)

        # Original data should remain
        self.assertEqual(result["symbol"], "BTCUSDT")
        self.assertEqual(result["price"], 50000)

    def test_remove_trace_headers_when_none_exist(self):
        """Test removing trace headers when message has none."""
        message_dict = {"symbol": "BTCUSDT", "price": 50000}

        result = NATSTracePropagator.remove_trace_headers(message_dict)

        # Should not raise error
        self.assertNotIn(NATSTracePropagator.TRACE_HEADERS_FIELD, result)
        self.assertEqual(result["symbol"], "BTCUSDT")
        self.assertEqual(result["price"], 50000)

    def test_trace_headers_field_name(self):
        """Test that the trace headers field name is as expected."""
        self.assertEqual(NATSTracePropagator.TRACE_HEADERS_FIELD, "_otel_trace_headers")

    def test_inject_context_handles_exceptions(self):
        """Test that inject_context handles exceptions gracefully."""
        message_dict = {"symbol": "BTCUSDT"}

        # Mock inject to raise an exception
        with patch(
            "data_manager.utils.nats_trace_propagator.inject",
            side_effect=Exception("Test error"),
        ):
            # Should not raise, should log warning
            result = NATSTracePropagator.inject_context(message_dict)

            # Should still return the message dict
            self.assertEqual(result["symbol"], "BTCUSDT")

    def test_extract_context_handles_exceptions(self):
        """Test that extract_context handles exceptions gracefully."""
        message_dict = {
            "symbol": "BTCUSDT",
            NATSTracePropagator.TRACE_HEADERS_FIELD: {"traceparent": "invalid"},
        }

        # Mock extract to raise an exception
        with patch(
            "data_manager.utils.nats_trace_propagator.extract",
            side_effect=Exception("Test error"),
        ):
            # Should not raise, should log warning and return None
            result = NATSTracePropagator.extract_context(message_dict)
            self.assertIsNone(result)

    def test_end_to_end_trace_propagation(self):
        """Test complete end-to-end trace propagation scenario."""
        # Simulate producer service creating a span and publishing message
        with self.tracer.start_as_current_span("producer_span") as producer_span:
            producer_trace_id = producer_span.get_span_context().trace_id
            producer_span_id = producer_span.get_span_context().span_id

            # Prepare message and inject trace context
            message = {"symbol": "BTCUSDT", "price": 50000, "event": "trade"}
            message_with_trace = NATSTracePropagator.inject_context(message)

            # Verify trace headers were injected
            self.assertIn(NATSTracePropagator.TRACE_HEADERS_FIELD, message_with_trace)

        # Simulate consumer service receiving message and processing
        ctx = NATSTracePropagator.extract_context(message_with_trace)
        self.assertIsNotNone(ctx, "Should extract valid context")

        with self.tracer.start_as_current_span(
            "consumer_span", context=ctx
        ) as consumer_span:
            consumer_trace_id = consumer_span.get_span_context().trace_id
            consumer_span_context = consumer_span.get_span_context()

            # Process message (simulated)
            processed = message_with_trace.copy()
            NATSTracePropagator.remove_trace_headers(processed)
            self.assertEqual(processed["symbol"], "BTCUSDT")

            # Verify consumer span is valid
            self.assertTrue(consumer_span_context.is_valid)
            self.assertEqual(consumer_span.name, "consumer_span")

        # Verify trace IDs match (same distributed trace)
        self.assertEqual(
            producer_trace_id,
            consumer_trace_id,
            "Consumer span should have same trace ID as producer (distributed trace)",
        )


if __name__ == "__main__":
    unittest.main()
