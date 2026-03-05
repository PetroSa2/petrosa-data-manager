"""
Tests for main.py OpenTelemetry initialization logic.
"""

import logging
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
@patch("data_manager.main.DataManagerApp")
@patch("data_manager.main.constants")
async def test_main_otel_enabled_with_endpoint(mock_constants, mock_app_class, caplog):
    """Test main() with OTEL enabled and endpoint configured."""
    # Setup constants
    mock_constants.OTEL_ENABLED = True
    mock_constants.OTEL_EXPORTER_OTLP_ENDPOINT = "http://grafana-alloy:4317"
    mock_constants.OTEL_SERVICE_NAME = "petrosa-data-manager"

    # Setup environment variables directly
    with patch.dict(
        os.environ,
        {
            "OTEL_ENABLED": "true",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://grafana-alloy:4317",
            "OTEL_SERVICE_NAME": "petrosa-data-manager",
            "OTEL_NO_AUTO_INIT": "1",  # Ensure auto-init is off for controlled testing
        },
    ):
        mock_app_instance = MagicMock()
        mock_app_instance.start = AsyncMock()
        mock_app_instance.stop = AsyncMock()
        mock_app_class.return_value = mock_app_instance

        # Mock asyncio context to prevent actual app startup
        mock_app_instance.start.side_effect = KeyboardInterrupt()

        with patch("data_manager.main.setup_telemetry") as mock_init_telemetry:
            with patch(
                "data_manager.main.attach_logging_handler"
            ) as mock_attach_handler:
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
                assert "OpenTelemetry logging handler attached" in "".join(log_messages)


@pytest.mark.asyncio
@patch("data_manager.main.DataManagerApp")
async def test_main_otel_disabled(mock_app_class, caplog):
    """Test main() with OTEL disabled."""
    # Setup environment variables directly for testing main's logic
    with patch.dict(
        os.environ,
        {
            "OTEL_ENABLED": "false",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "",
            "OTEL_SERVICE_NAME": "petrosa-data-manager",
            "OTEL_NO_AUTO_INIT": "1",  # Ensure auto-init is off for controlled testing
        },
    ):
        mock_app_instance = MagicMock()
        mock_app_instance.start = AsyncMock()
        mock_app_instance.stop = AsyncMock()
        mock_app_class.return_value = mock_app_instance

        # Mock asyncio context to prevent actual app startup
        mock_app_instance.start.side_effect = KeyboardInterrupt()

        # Patch main's references to setup_telemetry and attach_logging_handler
        # And patch constants used in main.py
        with patch("data_manager.main.constants.OTEL_ENABLED", False):
            with patch("data_manager.main.constants.OTEL_EXPORTER_OTLP_ENDPOINT", ""):
                with patch("data_manager.main.setup_telemetry") as mock_init_telemetry:
                    with patch(
                        "data_manager.main.attach_logging_handler"
                    ) as mock_attach_handler:
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


@pytest.mark.asyncio
@patch("data_manager.main.DataManagerApp")
async def test_main_otel_endpoint_missing(mock_app_class, caplog):
    """Test main() with OTEL enabled but no endpoint configured."""
    # Setup environment variables directly
    with patch.dict(
        os.environ,
        {
            "OTEL_ENABLED": "true",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "",  # Empty endpoint
            "OTEL_SERVICE_NAME": "petrosa-data-manager",
            "OTEL_NO_AUTO_INIT": "1",  # Ensure auto-init is off for controlled testing
        },
    ):
        mock_app_instance = MagicMock()
        mock_app_instance.start = AsyncMock()
        mock_app_instance.stop = AsyncMock()
        mock_app_class.return_value = mock_app_instance

        # Mock asyncio context to prevent actual app startup
        mock_app_instance.start.side_effect = KeyboardInterrupt()

        # Patch main's references to setup_telemetry and attach_logging_handler
        # And patch constants used in main.py
        with patch("data_manager.main.constants.OTEL_ENABLED", True):
            with patch("data_manager.main.constants.OTEL_EXPORTER_OTLP_ENDPOINT", ""):
                with patch("data_manager.main.setup_telemetry") as mock_init_telemetry:
                    with patch(
                        "data_manager.main.attach_logging_handler"
                    ) as mock_attach_handler:
                        # Mock attach_logging_handler to return False, simulating failure due to missing endpoint
                        mock_attach_handler.return_value = False
                        with caplog.at_level(logging.ERROR):
                            # Import and run main
                            from data_manager.main import main

                            try:
                                await main()
                            except KeyboardInterrupt:
                                pass  # Expected

                        # Verify OTEL initialization was called (endpoint check happens inside)
                        mock_init_telemetry.assert_called_once_with(
                            service_name="petrosa-data-manager",
                            service_type="async",
                            enable_mongodb=True,
                            enable_http=True,
                        )

                        # Verify logging handler was called, but returned False
                        mock_attach_handler.assert_called_once()

                        # Verify error message about missing endpoint (from main.py's logic)
                        log_messages = [record.message for record in caplog.records]
                        assert any(
                            "❌ Failed to attach OpenTelemetry logging handler" in msg
                            for msg in log_messages
                        )


@pytest.mark.asyncio
@patch("data_manager.main.DataManagerApp")
async def test_main_otel_import_error(mock_app_class, caplog):
    """Test main() when petrosa_otel package is not available."""
    # Setup environment variables directly
    with patch.dict(
        os.environ,
        {
            "OTEL_ENABLED": "true",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://grafana-alloy:4317",
            "OTEL_SERVICE_NAME": "petrosa-data-manager",
            "OTEL_NO_AUTO_INIT": "1",  # Ensure auto-init is off for controlled testing
        },
    ):
        mock_app_instance = MagicMock()
        mock_app_instance.start = AsyncMock()
        mock_app_instance.stop = AsyncMock()
        mock_app_class.return_value = mock_app_instance

        # Mock asyncio context to prevent actual app startup
        mock_app_instance.start.side_effect = KeyboardInterrupt()

        # Mock ImportError for petrosa_otel by setting them to None
        with patch("data_manager.main.setup_telemetry", None):
            with patch("data_manager.main.attach_logging_handler", None):
                with caplog.at_level(logging.INFO):
                    # Import and run main
                    from data_manager.main import main

                    try:
                        await main()
                    except KeyboardInterrupt:
                        pass  # Expected

            # Verify NO "Initializing OpenTelemetry" message (since setup_telemetry is None)
            log_messages = [record.message for record in caplog.records]
            assert not any("Initializing OpenTelemetry" in msg for msg in log_messages)
