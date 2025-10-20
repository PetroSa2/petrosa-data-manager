"""
OpenTelemetry initialization for the Petrosa Data Manager service.
"""

import logging

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

import constants

logger = logging.getLogger(__name__)


def init_telemetry() -> None:
    """Initialize OpenTelemetry tracing and metrics."""
    if not constants.OTEL_ENABLED:
        logger.info("OpenTelemetry is disabled")
        return

    if not constants.OTEL_EXPORTER_OTLP_ENDPOINT:
        logger.warning("OTEL_EXPORTER_OTLP_ENDPOINT not set, skipping telemetry initialization")
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
        trace_exporter = OTLPSpanExporter(endpoint=constants.OTEL_EXPORTER_OTLP_ENDPOINT)
        trace_provider.add_span_processor(BatchSpanProcessor(trace_exporter))
        trace.set_tracer_provider(trace_provider)

        # Initialize metrics
        metric_reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=constants.OTEL_EXPORTER_OTLP_ENDPOINT),
            export_interval_millis=60000,  # Export every 60 seconds
        )
        meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
        metrics.set_meter_provider(meter_provider)

        logger.info(
            "OpenTelemetry initialized successfully",
            extra={
                "service_name": constants.OTEL_SERVICE_NAME,
                "endpoint": constants.OTEL_EXPORTER_OTLP_ENDPOINT,
            },
        )
    except Exception as e:
        logger.error(f"Failed to initialize OpenTelemetry: {e}")


# Initialize on module import
if constants.OTEL_ENABLED and constants.OTEL_EXPORTER_OTLP_ENDPOINT:
    init_telemetry()

