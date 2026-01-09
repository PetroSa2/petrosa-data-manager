"""
OpenTelemetry initialization for the Petrosa Data Manager service.
"""

import logging
import os

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (
    OTLPLogExporter,
)
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
    OTLPMetricExporter,
)
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
    OTLPSpanExporter,
)
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

# Import AttributeFilterSpanProcessor from petrosa-otel for MongoDB compatibility
try:
    from petrosa_otel.processors import AttributeFilterSpanProcessor
except ImportError:
    # Fallback: Define locally if petrosa-otel not available
    class AttributeFilterSpanProcessor(BatchSpanProcessor):
        """Custom span processor that filters out invalid attribute values."""

        def on_start(self, span, parent_context=None):
            """Clean attributes when span starts."""
            super().on_start(span, parent_context)
            self._clean_attributes(span)

        def on_end(self, span):
            """Clean attributes when span ends."""
            self._clean_attributes(span)
            super().on_end(span)

        def _clean_attributes(self, span):
            """Remove invalid attribute values from span."""
            if not hasattr(span, "_attributes") or not span._attributes:
                return

            # Identify invalid attributes (dict/list values)
            invalid_keys = []
            for key, value in span._attributes.items():
                if isinstance(value, (dict, list)):
                    invalid_keys.append(key)

            # Remove invalid attributes
            for key in invalid_keys:
                del span._attributes[key]

# Import PyMongo instrumentation
try:
    from opentelemetry.instrumentation.pymongo import PymongoInstrumentor

    PYMONGO_INSTRUMENTATION_AVAILABLE = True
except ImportError:
    PymongoInstrumentor = None  # type: ignore
    PYMONGO_INSTRUMENTATION_AVAILABLE = False

import constants

logger = logging.getLogger(__name__)

# Global logger provider for attaching handlers
_global_logger_provider = None
_otlp_logging_handler = None


def init_telemetry() -> None:
    """Initialize OpenTelemetry tracing, metrics, and logging."""
    global _global_logger_provider

    if not constants.OTEL_ENABLED:
        logger.info("OpenTelemetry is disabled")
        return

    if not constants.OTEL_EXPORTER_OTLP_ENDPOINT:
        logger.warning(
            "OTEL_EXPORTER_OTLP_ENDPOINT not set, skipping telemetry initialization"
        )
        return

    try:
        # Create resource with service information
        resource = Resource.create(
            {
                "service.name": constants.OTEL_SERVICE_NAME,
                "service.version": constants.SERVICE_VERSION,
                "deployment.environment": constants.ENVIRONMENT,
            }
        )

        # Initialize tracing
        trace_provider = TracerProvider(resource=resource)
        trace_exporter = OTLPSpanExporter(
            endpoint=constants.OTEL_EXPORTER_OTLP_ENDPOINT
        )

        # Use AttributeFilterSpanProcessor to prevent errors from dict/list attributes
        # in MongoDB spans (required when MongoDB instrumentation is enabled)
        trace_provider.add_span_processor(
            AttributeFilterSpanProcessor(trace_exporter)
        )
        trace.set_tracer_provider(trace_provider)
        logger.info("OpenTelemetry tracing initialized")

        # Enable MongoDB instrumentation
        if PYMONGO_INSTRUMENTATION_AVAILABLE:
            try:
                PymongoInstrumentor().instrument()
                logger.info("✅ MongoDB instrumentation enabled")
            except Exception as e:
                logger.warning(f"⚠️  Failed to instrument MongoDB: {e}")
        else:
            logger.warning(
                "⚠️  opentelemetry-instrumentation-pymongo not installed. "
                "Install with: pip install opentelemetry-instrumentation-pymongo"
            )

        # Initialize metrics
        metric_reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=constants.OTEL_EXPORTER_OTLP_ENDPOINT),
            export_interval_millis=60000,  # Export every 60 seconds
        )
        meter_provider = MeterProvider(
            resource=resource, metric_readers=[metric_reader]
        )
        metrics.set_meter_provider(meter_provider)
        logger.info("OpenTelemetry metrics initialized")

        # Initialize logging export
        try:
            # Enrich logs with trace context
            # set_logging_format=False to avoid clearing existing handlers
            LoggingInstrumentor().instrument(set_logging_format=False)

            log_exporter = OTLPLogExporter(
                endpoint=constants.OTEL_EXPORTER_OTLP_ENDPOINT,
            )

            logger_provider = LoggerProvider(resource=resource)
            logger_provider.add_log_record_processor(
                BatchLogRecordProcessor(log_exporter)
            )
            _global_logger_provider = logger_provider

            logger.info("OpenTelemetry logging export configured")
            logger.info(
                "   Note: Call attach_logging_handler_simple() in main() to activate"
            )

        except Exception as e:
            logger.error(f"Failed to set up OpenTelemetry logging export: {e}")

        logger.info(
            "OpenTelemetry initialized successfully",
            extra={
                "service_name": constants.OTEL_SERVICE_NAME,
                "endpoint": constants.OTEL_EXPORTER_OTLP_ENDPOINT,
            },
        )
    except Exception as e:
        logger.error(f"Failed to initialize OpenTelemetry: {e}")


def attach_logging_handler_simple():
    """
    Attach OTLP logging handler to root logger.

    For async services without Uvicorn (NATS listeners, async processors).
    This attaches the OTLP handler to the root logger only.

    Call this in main() after init_telemetry() to activate log export.
    """
    global _global_logger_provider, _otlp_logging_handler

    if _global_logger_provider is None:
        logger.warning("Logger provider not configured - logging export not available")
        return False

    try:
        root_logger = logging.getLogger()

        # Check if handler already attached
        if _otlp_logging_handler is not None:
            if _otlp_logging_handler in root_logger.handlers:
                logger.info("OTLP logging handler already attached")
                return True

        # Create and attach handler
        handler = LoggingHandler(
            level=logging.NOTSET,
            logger_provider=_global_logger_provider,
        )

        root_logger.addHandler(handler)
        _otlp_logging_handler = handler

        logger.info("OTLP logging handler attached to root logger")
        logger.info(f"   Total handlers: {len(root_logger.handlers)}")

        return True

    except Exception as e:
        logger.error(f"Failed to attach logging handler: {e}")
        return False


def get_tracer(name: str = None) -> trace.Tracer:
    """
    Get a tracer instance.

    Args:
        name: Tracer name

    Returns:
        Tracer instance
    """
    return trace.get_tracer(name or constants.OTEL_SERVICE_NAME)


def get_meter(name: str = None) -> metrics.Meter:
    """
    Get a meter instance.

    Args:
        name: Meter name

    Returns:
        Meter instance
    """
    return metrics.get_meter(name or constants.OTEL_SERVICE_NAME)


# Initialize on module import (unless explicitly disabled for testing)
if (
    constants.OTEL_ENABLED
    and constants.OTEL_EXPORTER_OTLP_ENDPOINT
    and not os.getenv("OTEL_NO_AUTO_INIT")
):
    init_telemetry()
