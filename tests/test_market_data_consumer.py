"""
Tests for the market data consumer.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime
import json

from data_manager.consumer.market_data_consumer import MarketDataConsumer
from data_manager.consumer.nats_client import NATSClient
from data_manager.consumer.message_handler import MessageHandler
from data_manager.models.events import MarketDataEvent, EventType

@pytest.fixture
def mock_nats_client():
    """Mock NATSClient."""
    return AsyncMock(spec=NATSClient)

@pytest.fixture
def mock_message_handler():
    """Mock MessageHandler."""
    return AsyncMock(spec=MessageHandler)

@pytest.fixture
def market_data_consumer(mock_nats_client, mock_message_handler):
    """Create a MarketDataConsumer instance with mocks."""
    consumer = MarketDataConsumer(nats_client=mock_nats_client, message_handler=mock_message_handler)
    consumer.running = True # Set to running for tests that need the loop
    return consumer

@pytest.mark.asyncio
async def test_process_message_invalid_event_logging(market_data_consumer):
    """Test that invalid messages are logged correctly with structured logging."""
    mock_msg = MagicMock()
    # Malformed data that will cause MarketDataEvent.from_nats_message to return None
    malformed_data = {"some_key": "some_value", "stream": "test_stream"}
    mock_msg.data.decode.return_value = json.dumps(malformed_data)
    mock_msg.subject = "test.subject.invalid" # Add a subject to the mock NATS message

    # Mock MarketDataEvent.from_nats_message to return None
    with patch("data_manager.consumer.market_data_consumer.MarketDataEvent.from_nats_message", return_value=None):
        with patch("data_manager.consumer.market_data_consumer.logger") as mock_logger:
            await market_data_consumer._process_message(mock_msg)

            mock_logger.warning.assert_called_once_with(
                "invalid_message_received",
                extra={
                    "subject": mock_msg.subject,
                    "raw_data": malformed_data,
                },
            )
            # Ensure message handler was NOT called
            market_data_consumer.message_handler.handle_event.assert_not_called()
