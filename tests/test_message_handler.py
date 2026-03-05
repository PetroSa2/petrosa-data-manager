"""
Tests for the message handler.
"""

from datetime import datetime
from unittest.mock import AsyncMock, patch, ANY

import pytest

from data_manager.consumer.message_handler import MessageHandler
from data_manager.models.events import EventType, MarketDataEvent


@pytest.fixture
def message_handler():
    """Create a new message handler instance."""
    return MessageHandler()


@pytest.mark.asyncio
async def test_message_handler_initialization(message_handler):
    """Test message handler initialization."""
    assert not message_handler.initialized
    await message_handler.initialize()
    assert message_handler.initialized

    # Check stats
    stats = message_handler.get_stats()
    assert stats["unknown"] == 0
    assert stats["trades"] == 0


@pytest.mark.asyncio
async def test_handle_unknown_event(message_handler):
    """Test handling of unknown events with structured logging."""
    await message_handler.initialize()

    # Create an unknown event
    event = MarketDataEvent(
        event_type=EventType.UNKNOWN,
        symbol="BTCUSDT",
        timestamp=datetime.utcnow(),
        data={"strange_key": "some_value"},
        exchange="binance",
        stream="mystream",
    )

    with patch("data_manager.consumer.message_handler.logger") as mock_logger:
        await message_handler._handle_unknown(event)

        # Verify stats updated
        stats = message_handler.get_stats()
        assert stats["unknown"] == 1

        # Verify structured logger called correctly
        mock_logger.warning.assert_called_once_with(
            "received_unknown_event",
            extra={
                "subject": "mystream",
                "event_type": "unknown",
                "symbol": "BTCUSDT",
                "exchange": "binance",
                "timestamp": ANY,
                "data_keys": ["strange_key"],
                "raw_data": {"strange_key": "some_value"},
            },
        )


@pytest.mark.asyncio
async def test_handle_unknown_event_no_stream(message_handler):
    """Test handling of unknown events without a stream."""
    await message_handler.initialize()

    # Create an unknown event with no stream
    event = MarketDataEvent(
        event_type=EventType.UNKNOWN,
        symbol="BTCUSDT",
        timestamp=datetime.utcnow(),
        data={"another_key": 123},
        exchange="binance",
        stream=None,
    )

    with patch("data_manager.consumer.message_handler.logger") as mock_logger:
        await message_handler._handle_unknown(event)

        # Verify stats updated
        stats = message_handler.get_stats()
        assert stats["unknown"] == 1

        # Verify structured logger called correctly with "unknown" fallback subject
        mock_logger.warning.assert_called_once_with(
            "received_unknown_event",
            extra={
                "subject": "unknown",
                "event_type": "unknown",
                "symbol": "BTCUSDT",
                "exchange": "binance",
                "timestamp": ANY,
                "data_keys": ["another_key"],
                "raw_data": {"another_key": 123},
            },
        )
