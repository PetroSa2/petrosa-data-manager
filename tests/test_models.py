"""
Tests for data models.
"""

from datetime import datetime
from decimal import Decimal

from data_manager.models.events import EventType, MarketDataEvent
from data_manager.models.market_data import Candle, Trade


def test_market_data_event_from_nats_message():
    """Test parsing NATS message into MarketDataEvent."""
    msg_data = {
        "e": "trade",
        "s": "BTCUSDT",
        "p": "50000.00",
        "q": "0.1",
        "T": 1633046400000,
    }

    event = MarketDataEvent.from_nats_message(msg_data)

    assert event.event_type == EventType.TRADE
    assert event.symbol == "BTCUSDT"
    assert isinstance(event.timestamp, datetime)
    assert event.data == msg_data


def test_candle_model():
    """Test Candle model validation."""
    candle = Candle(
        symbol="BTCUSDT",
        timestamp=datetime.utcnow(),
        open=Decimal("50000.00"),
        high=Decimal("51000.00"),
        low=Decimal("49500.00"),
        close=Decimal("50500.00"),
        volume=Decimal("1000.0"),
        timeframe="1h",
    )

    assert candle.symbol == "BTCUSDT"
    assert candle.open == Decimal("50000.00")
    assert candle.timeframe == "1h"


def test_trade_model():
    """Test Trade model validation."""
    trade = Trade(
        symbol="BTCUSDT",
        trade_id=12345,
        timestamp=datetime.utcnow(),
        price=Decimal("50000.00"),
        quantity=Decimal("0.1"),
        quote_quantity=Decimal("5000.00"),
        is_buyer_maker=True,
        side="buy",
    )

    assert trade.symbol == "BTCUSDT"
    assert trade.trade_id == 12345
    assert trade.side == "buy"
