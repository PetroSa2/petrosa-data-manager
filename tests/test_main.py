"""
Tests for main.py OpenTelemetry initialization logic.
"""

import logging
import os
from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.asyncio
@patch("data_manager.main.constants")
@patch("data_manager.main.DataManagerApp")
async def test_main_otel_enabled_with_endpoint(mock_app_class, mock_constants, caplog):
    """Test main() with OTEL enabled and endpoint configured."""
    # Setup
    mock_constants.OTEL_ENABLED = True
    mock_constants.OTEL_EXPORTER_OTLP_ENDPOINT = "http://grafana-alloy:4317"
    mock_constants.OTEL_SERVICE_NAME = "petrosa-data-manager"

    mock_app_instance = MagicMock()
    mock_app_instance.start = MagicMock()
    mock_app_instance.stop = MagicMock()
    mock_app_class.return_value = mock_app_instance

    # Mock asyncio context to prevent actual app startup
    mock_app_instance.start.side_effect = KeyboardInterrupt()

    with patch(
        "data_manager.main.initialize_telemetry_standard"
    ) as mock_init_telemetry:
        with patch("data_manager.main.attach_logging_handler") as mock_attach_handler:
            with caplog.at_level(logging.INFO):
                # Import and run main
                from data_manager.main import main

                try:
                    await main()
                except KeyboardInterrupt:
                    pass  # Expected

            # Verify OTEL initialization was called
            mock_init_telemetry.assert_called_once_with(
                service_name="petrosa-data-manager",
                service_type="async",
                enable_mongodb=True,
                enable_http=True,
            )

            # Verify logging handler was attached
            mock_attach_handler.assert_called_once()

            # Verify logging messages
            log_messages = [record.message for record in caplog.records]
            assert any("Initializing OpenTelemetry" in msg for msg in log_messages)
            assert any(
                "OpenTelemetry logging handler attached" in msg for msg in log_messages
            )


@pytest.mark.asyncio
@patch("data_manager.main.constants")
@patch("data_manager.main.DataManagerApp")
async def test_main_otel_disabled(mock_app_class, mock_constants, caplog):
    """Test main() with OTEL disabled."""
    # Setup
    mock_constants.OTEL_ENABLED = False
    mock_constants.OTEL_EXPORTER_OTLP_ENDPOINT = ""
    mock_constants.OTEL_SERVICE_NAME = "petrosa-data-manager"

    mock_app_instance = MagicMock()
    mock_app_instance.start = MagicMock()
    mock_app_instance.stop = MagicMock()
    mock_app_class.return_value = mock_app_instance

    # Mock asyncio context to prevent actual app startup
    mock_app_instance.start.side_effect = KeyboardInterrupt()

    with patch(
        "data_manager.main.initialize_telemetry_standard"
    ) as mock_init_telemetry:
        with patch("data_manager.main.attach_logging_handler") as mock_attach_handler:
            with caplog.at_level(logging.INFO):
                # Import and run main
                from data_manager.main import main

                try:
                    await main()
                except KeyboardInterrupt:
                    pass  # Expected

            # Verify OTEL was NOT initialized
            mock_init_telemetry.assert_not_called()
            mock_attach_handler.assert_not_called()

            # Verify warning message
            log_messages = [record.message for record in caplog.records]
            assert any("OpenTelemetry is disabled" in msg for msg in log_messages)


@pytest.mark.asyncio
@patch("data_manager.main.constants")
@patch("data_manager.main.DataManagerApp")
async def test_main_otel_endpoint_missing(mock_app_class, mock_constants, caplog):
    """Test main() with OTEL enabled but no endpoint configured."""
    # Setup
    mock_constants.OTEL_ENABLED = True
    mock_constants.OTEL_EXPORTER_OTLP_ENDPOINT = ""  # Empty endpoint
    mock_constants.OTEL_SERVICE_NAME = "petrosa-data-manager"

    mock_app_instance = MagicMock()
    mock_app_instance.start = MagicMock()
    mock_app_instance.stop = MagicMock()
    mock_app_class.return_value = mock_app_instance

    # Mock asyncio context to prevent actual app startup
    mock_app_instance.start.side_effect = KeyboardInterrupt()

    with patch(
        "data_manager.main.initialize_telemetry_standard"
    ) as mock_init_telemetry:
        with patch("data_manager.main.attach_logging_handler") as mock_attach_handler:
            with caplog.at_level(logging.ERROR):
                # Import and run main
                from data_manager.main import main

                try:
                    await main()
                except KeyboardInterrupt:
                    pass  # Expected

            # Verify OTEL initialization was called (endpoint check happens inside)
            mock_init_telemetry.assert_called_once()

            # Verify logging handler was NOT attached due to missing endpoint
            mock_attach_handler.assert_not_called()

            # Verify error message about missing endpoint
            log_messages = [record.message for record in caplog.records]
            assert any(
                "OTEL_EXPORTER_OTLP_ENDPOINT is empty" in msg for msg in log_messages
            )


@pytest.mark.asyncio
@patch("data_manager.main.constants")
@patch("data_manager.main.DataManagerApp")
async def test_main_otel_import_error(mock_app_class, mock_constants, caplog):
    """Test main() when petrosa_otel package is not available."""
    # Setup
    mock_constants.OTEL_ENABLED = True
    mock_constants.OTEL_EXPORTER_OTLP_ENDPOINT = "http://grafana-alloy:4317"
    mock_constants.OTEL_SERVICE_NAME = "petrosa-data-manager"

    mock_app_instance = MagicMock()
    mock_app_instance.start = MagicMock()
    mock_app_instance.stop = MagicMock()
    mock_app_class.return_value = mock_app_instance

    # Mock asyncio context to prevent actual app startup
    mock_app_instance.start.side_effect = KeyboardInterrupt()

    # Mock ImportError for petrosa_otel
    with patch(
        "data_manager.main.initialize_telemetry_standard",
        side_effect=ImportError("No module named 'petrosa_otel'"),
    ):
        with caplog.at_level(logging.WARNING):
            # Import and run main
            from data_manager.main import main

            try:
                await main()
            except KeyboardInterrupt:
                pass  # Expected

        # Verify warning message about missing package
        log_messages = [record.message for record in caplog.records]
        assert any("petrosa_otel package not available" in msg for msg in log_messages)
