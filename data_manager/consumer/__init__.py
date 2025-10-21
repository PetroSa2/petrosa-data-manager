"""
NATS consumer for market data events.
"""

from data_manager.consumer.market_data_consumer import MarketDataConsumer
from data_manager.consumer.nats_client import NATSClient

__all__ = ["NATSClient", "MarketDataConsumer"]
