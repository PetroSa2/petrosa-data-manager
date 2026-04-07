"""
Tests for data models.
"""

from datetime import datetime, timezone

try:
    from datetime import UTC
except ImportError:
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017
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

    # Test 24hrminiticker
    msg_ticker = {"e": "24hrminiticker", "s": "BTCUSDT", "c": "50500.00"}
    event_ticker = MarketDataEvent.from_nats_message(msg_ticker)
    assert event_ticker.event_type == EventType.TICKER

    # Test markpriceupdate
    msg_mark = {"e": "markpriceupdate", "s": "BTCUSDT", "p": "50000.00"}
    event_mark = MarketDataEvent.from_nats_message(msg_mark)
    assert event_mark.event_type == EventType.MARK_PRICE

    # Test depthupdate
    msg_depth = {"e": "depthupdate", "s": "BTCUSDT"}
    event_depth = MarketDataEvent.from_nats_message(msg_depth)
    assert event_depth.event_type == EventType.DEPTH

    # Test kline
    msg_kline = {"e": "kline", "s": "BTCUSDT"}
    event_kline = MarketDataEvent.from_nats_message(msg_kline)
    assert event_kline.event_type == EventType.CANDLE

    # Test top-level stream parsing (coverage for lines 68-81, 92-97)
    msg_stream_trade = {"stream": "btcusdt@trade"}
    event_stream_trade = MarketDataEvent.from_nats_message(msg_stream_trade)
    assert event_stream_trade.event_type == EventType.TRADE
    assert event_stream_trade.symbol == "BTCUSDT"

    msg_stream_ticker = {"stream": "btcusdt@ticker"}
    event_stream_ticker = MarketDataEvent.from_nats_message(msg_stream_ticker)
    assert event_stream_ticker.event_type == EventType.TICKER

    msg_stream_depth = {"stream": "btcusdt@depth"}
    event_stream_depth = MarketDataEvent.from_nats_message(msg_stream_depth)
    assert event_stream_depth.event_type == EventType.DEPTH

    msg_stream_markprice = {"stream": "btcusdt@markPrice"}
    event_stream_markprice = MarketDataEvent.from_nats_message(msg_stream_markprice)
    assert event_stream_markprice.event_type == EventType.MARK_PRICE

    msg_stream_funding = {"stream": "btcusdt@fundingRate"}
    event_stream_funding = MarketDataEvent.from_nats_message(msg_stream_funding)
    assert event_stream_funding.event_type == EventType.FUNDING_RATE

    msg_stream_kline = {"stream": "btcusdt@kline_1m"}
    event_stream_kline = MarketDataEvent.from_nats_message(msg_stream_kline)
    assert event_stream_kline.event_type == EventType.CANDLE

    msg_stream_invalid = {"stream": "invalid"}
    assert MarketDataEvent.from_nats_message(msg_stream_invalid) is None


def test_candle_model():
    """Test Candle model validation."""
    candle = Candle(
        symbol="BTCUSDT",
        timestamp=datetime.now(UTC),
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
        timestamp=datetime.now(UTC),
        price=Decimal("50000.00"),
        quantity=Decimal("0.1"),
        quote_quantity=Decimal("5000.00"),
        is_buyer_maker=True,
        side="buy",
    )

    assert trade.symbol == "BTCUSDT"
    assert trade.trade_id == 12345
    assert trade.side == "buy"
