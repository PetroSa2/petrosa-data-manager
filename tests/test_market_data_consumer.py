"""
Tests for market_data_consumer module.

Tests the MarketDataConsumer's message processing with proper
trace context propagation.
"""

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from data_manager.consumer.market_data_consumer import MarketDataConsumer


class TestMarketDataConsumer:
    """Tests for MarketDataConsumer."""

    @pytest.fixture
    def setup_tracing(self):
        """Setup OpenTelemetry tracing for tests."""
        provider = TracerProvider()
        exporter = InMemorySpanExporter()
        processor = SimpleSpanProcessor(exporter)
        provider.add_span_processor(processor)
        trace.set_tracer_provider(provider)

        yield exporter

        # Cleanup
        exporter.clear()

    @pytest.mark.asyncio
    async def test_process_message_with_span_kind_parameter(self, setup_tracing):
        """
        Test that _process_message calls create_span_from_message with span_kind parameter.

        This test specifically covers the fix for issue #27 where the parameter
        name was corrected from 'kind' to 'span_kind'.
        """
        exporter = setup_tracing

        # Create mock NATS message
        mock_msg = Mock()
        sample_data = {
            "symbol": "BTCUSDT",
            "event_type": "trade",
            "price": "50000.00",
            "quantity": "0.5",
            "timestamp": 1234567890000,
        }
        mock_msg.data = json.dumps(sample_data).encode()

        # Create mocked dependencies
        mock_nats = Mock()
        mock_db = Mock()
        mock_handler = AsyncMock()
        mock_handler.handle_event = AsyncMock()

        # Create consumer instance with mocked dependencies
        consumer = MarketDataConsumer(
            nats_client=mock_nats,
            message_handler=mock_handler,
            db_manager=mock_db,
        )

        # Process the message
        await consumer._process_message(mock_msg)

        # Verify span was created
        spans = exporter.get_finished_spans()
        assert len(spans) >= 1

        # Verify the span has the correct kind (CONSUMER)
        process_span = next(
            (s for s in spans if s.name == "process_nats_message"), None
        )
        assert process_span is not None
        assert process_span.kind == trace.SpanKind.CONSUMER

        # Verify span attributes
        assert "messaging.destination" in process_span.attributes
        assert process_span.attributes.get("messaging.system") == "nats"

    @pytest.mark.asyncio
    async def test_process_message_with_invalid_data(self, setup_tracing):
        """Test that invalid messages are handled gracefully without raising exceptions."""
        exporter = setup_tracing

        # Create mock NATS message with invalid data (missing required fields)
        mock_msg = Mock()
        invalid_data = {"invalid": "data"}
        mock_msg.data = json.dumps(invalid_data).encode()

        # Create mocked dependencies
        mock_nats = Mock()
        mock_db = Mock()
        mock_handler = AsyncMock()
        mock_handler.handle_event = AsyncMock()

        # Create consumer instance with mocked dependencies
        consumer = MarketDataConsumer(
            nats_client=mock_nats,
            message_handler=mock_handler,
            db_manager=mock_db,
        )

        # Process the invalid message (should not raise exception)
        # This test verifies that the span_kind parameter is accepted even for invalid messages
        try:
            await consumer._process_message(mock_msg)
            # If we get here without exception, the test passes
            assert True
        except TypeError as e:
            # If we get a TypeError about 'kind' parameter, the fix didn't work
            if "unexpected keyword argument" in str(e):
                pytest.fail(f"Parameter name mismatch still exists: {e}")
            raise
