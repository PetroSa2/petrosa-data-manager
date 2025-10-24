"""
Binance REST API client for fetching historical data.
"""

import asyncio
import logging
from datetime import datetime

import httpx

import constants

logger = logging.getLogger(__name__)


class RateLimiter:
    """Simple token bucket rate limiter."""

    def __init__(self, requests_per_minute: int = 1200):
        """
        Initialize rate limiter.

        Args:
            requests_per_minute: Maximum requests per minute
        """
        self.requests_per_minute = requests_per_minute
        self.tokens = requests_per_minute
        self.last_update = datetime.utcnow()
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Acquire a token, waiting if necessary."""
        async with self.lock:
            now = datetime.utcnow()
            elapsed = (now - self.last_update).total_seconds()

            # Refill tokens based on elapsed time
            refill = elapsed * (self.requests_per_minute / 60.0)
            self.tokens = min(self.requests_per_minute, self.tokens + refill)
            self.last_update = now

            # Wait if no tokens available
            while self.tokens < 1:
                await asyncio.sleep(0.1)
                now = datetime.utcnow()
                elapsed = (now - self.last_update).total_seconds()
                refill = elapsed * (self.requests_per_minute / 60.0)
                self.tokens = min(self.requests_per_minute, self.tokens + refill)
                self.last_update = now

            self.tokens -= 1


class BinanceClient:
    """
    Binance REST API client for historical data fetching.

    Implements rate limiting and retry logic.
    """

    def __init__(self):
        """Initialize Binance client."""
        self.client = httpx.AsyncClient(timeout=30.0)
        self.rate_limiter = RateLimiter(
            requests_per_minute=constants.BINANCE_RATE_LIMIT
        )

    async def close(self) -> None:
        """Close the HTTP client."""
        await self.client.aclose()

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        start_time: datetime,
        end_time: datetime,
        limit: int = 1000,
    ) -> list[list]:
        """
        Fetch kline/candle data from Binance.

        Args:
            symbol: Trading pair symbol
            interval: Interval (e.g., '1m', '1h')
            start_time: Start datetime
            end_time: End datetime
            limit: Maximum number of klines (max 1000)

        Returns:
            List of kline arrays
        """
        await self.rate_limiter.acquire()

        start_ms = int(start_time.timestamp() * 1000)
        end_ms = int(end_time.timestamp() * 1000)

        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": min(limit, 1000),
        }

        # Try futures endpoint first, fallback to spot
        urls = [
            f"{constants.BINANCE_FAPI_BASE_URL}/fapi/v1/klines",
            f"{constants.BINANCE_API_BASE_URL}/api/v3/klines",
        ]

        for url in urls:
            try:
                response = await self.client.get(url, params=params)
                response.raise_for_status()
                data = response.json()

                logger.debug(
                    f"Fetched {len(data)} klines for {symbol} {interval} "
                    f"from {start_time} to {end_time}"
                )
                return data

            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404 and url == urls[0]:
                    # Try next URL (spot endpoint)
                    continue
                logger.error(f"HTTP error fetching klines: {e}")
                raise
            except Exception as e:
                logger.error(f"Error fetching klines from {url}: {e}")
                if url == urls[-1]:  # Last URL
                    raise

        return []

    async def get_funding_rate(
        self,
        symbol: str,
        start_time: datetime,
        end_time: datetime,
        limit: int = 1000,
    ) -> list[dict]:
        """
        Fetch funding rate data from Binance Futures.

        Args:
            symbol: Trading pair symbol
            start_time: Start datetime
            end_time: End datetime
            limit: Maximum number of records

        Returns:
            List of funding rate dictionaries
        """
        await self.rate_limiter.acquire()

        start_ms = int(start_time.timestamp() * 1000)
        end_ms = int(end_time.timestamp() * 1000)

        params = {
            "symbol": symbol,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": min(limit, 1000),
        }

        url = f"{constants.BINANCE_FAPI_BASE_URL}/fapi/v1/fundingRate"

        try:
            response = await self.client.get(url, params=params)
            response.raise_for_status()
            data = response.json()

            logger.debug(
                f"Fetched {len(data)} funding rates for {symbol} "
                f"from {start_time} to {end_time}"
            )
            return data

        except Exception as e:
            logger.error(f"Error fetching funding rates: {e}")
            return []

    async def get_agg_trades(
        self,
        symbol: str,
        start_time: datetime,
        end_time: datetime,
        limit: int = 1000,
    ) -> list[dict]:
        """
        Fetch aggregated trade data from Binance.

        Args:
            symbol: Trading pair symbol
            start_time: Start datetime
            end_time: End datetime
            limit: Maximum number of trades

        Returns:
            List of trade dictionaries
        """
        await self.rate_limiter.acquire()

        start_ms = int(start_time.timestamp() * 1000)
        end_ms = int(end_time.timestamp() * 1000)

        params = {
            "symbol": symbol,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": min(limit, 1000),
        }

        # Try futures endpoint first, fallback to spot
        urls = [
            f"{constants.BINANCE_FAPI_BASE_URL}/fapi/v1/aggTrades",
            f"{constants.BINANCE_API_BASE_URL}/api/v3/aggTrades",
        ]

        for url in urls:
            try:
                response = await self.client.get(url, params=params)
                response.raise_for_status()
                data = response.json()

                logger.debug(
                    f"Fetched {len(data)} trades for {symbol} "
                    f"from {start_time} to {end_time}"
                )
                return data

            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404 and url == urls[0]:
                    continue
                logger.error(f"HTTP error fetching trades: {e}")
                raise
            except Exception as e:
                logger.error(f"Error fetching trades from {url}: {e}")
                if url == urls[-1]:
                    raise

        return []
