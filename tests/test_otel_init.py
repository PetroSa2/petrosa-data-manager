"""
Tests for otel_init module MongoDB instrumentation.
"""

import importlib
import logging
import sys
from unittest.mock import MagicMock, patch

import pytest


class TestMongoDBInstrumentation:
    """Tests for MongoDB instrumentation in otel_init."""

    @patch("otel_init.constants")
    @patch("otel_init.PYMONGO_INSTRUMENTATION_AVAILABLE", True)
    @patch("otel_init.PymongoInstrumentor")
    def test_mongodb_instrumentation_enabled_when_available(
        self, mock_pymongo_instrumentor, mock_constants
    ):
        """Test that PyMongo instrumentation is enabled when available."""
        mock_constants.OTEL_ENABLED = True
        mock_constants.OTEL_EXPORTER_OTLP_ENDPOINT = "http://localhost:4317"
        mock_constants.OTEL_SERVICE_NAME = "test-service"
        mock_constants.SERVICE_VERSION = "1.0.0"
        mock_constants.ENVIRONMENT = "test"

        mock_instrumentor_instance = MagicMock()
        mock_pymongo_instrumentor.return_value = mock_instrumentor_instance

        # Import and call init_telemetry
        from otel_init import init_telemetry

        init_telemetry()

        # Verify PymongoInstrumentor was called
        mock_pymongo_instrumentor.assert_called_once()
        mock_instrumentor_instance.instrument.assert_called_once()

    @patch("otel_init.constants")
    @patch("otel_init.PYMONGO_INSTRUMENTATION_AVAILABLE", False)
    def test_mongodb_instrumentation_skipped_when_not_available(
        self, mock_constants, caplog
    ):
        """Test that MongoDB instrumentation is skipped when package not installed."""
        mock_constants.OTEL_ENABLED = True
        mock_constants.OTEL_EXPORTER_OTLP_ENDPOINT = "http://localhost:4317"
        mock_constants.OTEL_SERVICE_NAME = "test-service"
        mock_constants.SERVICE_VERSION = "1.0.0"
        mock_constants.ENVIRONMENT = "test"

        with caplog.at_level(logging.WARNING):
            from otel_init import init_telemetry

            init_telemetry()

            # Verify warning was logged
            assert any(
                "opentelemetry-instrumentation-pymongo not installed" in record.message
                for record in caplog.records
            )

    @patch("otel_init.constants")
    @patch("otel_init.PYMONGO_INSTRUMENTATION_AVAILABLE", True)
    @patch("otel_init.PymongoInstrumentor")
    def test_mongodb_instrumentation_handles_exception(
        self, mock_pymongo_instrumentor, mock_constants, caplog
    ):
        """Test that MongoDB instrumentation handles exceptions gracefully."""
        mock_constants.OTEL_ENABLED = True
        mock_constants.OTEL_EXPORTER_OTLP_ENDPOINT = "http://localhost:4317"
        mock_constants.OTEL_SERVICE_NAME = "test-service"
        mock_constants.SERVICE_VERSION = "1.0.0"
        mock_constants.ENVIRONMENT = "test"

        mock_instrumentor_instance = MagicMock()
        mock_instrumentor_instance.instrument.side_effect = Exception("Test error")
        mock_pymongo_instrumentor.return_value = mock_instrumentor_instance

        with caplog.at_level(logging.WARNING):
            from otel_init import init_telemetry

            init_telemetry()

            # Verify warning was logged
            assert any(
                "Failed to instrument MongoDB" in record.message
                for record in caplog.records
            )

    @patch("otel_init.constants")
    def test_attribute_filter_span_processor_used_when_mongodb_enabled(
        self, mock_constants
    ):
        """Test that AttributeFilterSpanProcessor is used when MongoDB instrumentation is enabled."""
        mock_constants.OTEL_ENABLED = True
        mock_constants.OTEL_EXPORTER_OTLP_ENDPOINT = "http://localhost:4317"
        mock_constants.OTEL_SERVICE_NAME = "test-service"
        mock_constants.SERVICE_VERSION = "1.0.0"
        mock_constants.ENVIRONMENT = "test"

        with patch("otel_init.TracerProvider") as mock_tracer_provider_class:
            mock_tracer_provider = MagicMock()
            mock_tracer_provider_class.return_value = mock_tracer_provider

            with patch("otel_init.AttributeFilterSpanProcessor") as mock_processor_class:
                from otel_init import init_telemetry

                init_telemetry()

                # Verify AttributeFilterSpanProcessor was used
                mock_processor_class.assert_called()
                mock_tracer_provider.add_span_processor.assert_called()


class TestAttributeFilterSpanProcessorFallback:
    """Tests for AttributeFilterSpanProcessor fallback implementation."""

    def test_fallback_uses_public_api_when_available(self):
        """Test that fallback uses public Span API when available."""
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        # Mock the import to trigger fallback
        with patch.dict("sys.modules", {"petrosa_otel": None}):
            # Force reimport to get fallback
            importlib.reload(sys.modules.get("otel_init", None) or __import__("otel_init"))

            # Get the fallback class
            from otel_init import AttributeFilterSpanProcessor

            # Create a mock span with public API
            mock_span = MagicMock()
            mock_span.attributes = {"valid": "value", "invalid_dict": {"key": "value"}}
            mock_span.set_attribute = MagicMock()

            # Create processor instance
            processor = AttributeFilterSpanProcessor(ConsoleSpanExporter())

            # Test _clean_attributes
            processor._clean_attributes(mock_span)

            # Verify set_attribute was called to remove invalid attribute
            mock_span.set_attribute.assert_called_with("invalid_dict", None)

    def test_fallback_handles_missing_public_api(self):
        """Test that fallback handles spans without public API gracefully."""
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        # Mock the import to trigger fallback
        with patch.dict("sys.modules", {"petrosa_otel": None}):
            importlib.reload(sys.modules.get("otel_init", None) or __import__("otel_init"))

            from otel_init import AttributeFilterSpanProcessor

            # Create a mock span without public API
            mock_span = MagicMock()
            del mock_span.attributes  # Remove public API
            mock_span._attributes = {"valid": "value", "invalid_list": [1, 2, 3]}

            processor = AttributeFilterSpanProcessor(ConsoleSpanExporter())

            # Should not raise exception
            processor._clean_attributes(mock_span)

            # Verify invalid attribute was removed
            assert "invalid_list" not in mock_span._attributes
            assert "valid" in mock_span._attributes
