"""
Data Manager Client - Main client class for interacting with the API.
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urljoin

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .exceptions import (
    APIError,
    ConnectionError as ClientConnectionError,
    TimeoutError as ClientTimeoutError,
    ValidationError,
)
from .models import (
    APIResponse,
    CandleData,
    DeleteOptions,
    DepthData,
    FundingData,
    InsertOptions,
    QueryOptions,
    TradeData,
    UpdateOptions,
)

logger = logging.getLogger(__name__)


class DataManagerClient:
    """
    Client for interacting with Petrosa Data Manager API.

    Supports both generic CRUD operations and domain-specific market data endpoints.
    Includes connection pooling, retries, and circuit breaker protection.

    Example:
        >>> client = DataManagerClient(base_url="http://data-manager:8000")
        >>> candles = await client.get_candles("BTCUSDT", "15m", limit=200)
        >>> await client.insert("mongodb", "trades_BTCUSDT", data=[...])
    """

    def __init__(
        self,
        base_url: str,
        timeout: int = 30,
        max_retries: int = 3,
        pool_size: int = 10,
        api_key: str | None = None,
    ):
        """
        Initialize Data Manager Client.

        Args:
            base_url: Base URL of the Data Manager API
            timeout: Request timeout in seconds
            max_retries: Maximum number of retry attempts
            pool_size: HTTP connection pool size
            api_key: Optional API key for authentication
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.api_key = api_key

        # Create HTTP client with connection pooling
        limits = httpx.Limits(
            max_keepalive_connections=pool_size, max_connections=pool_size * 2
        )
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            limits=limits,
            follow_redirects=True,
        )

        # Circuit breaker state
        self._circuit_breaker_failures = 0
        self._circuit_breaker_threshold = 5
        self._circuit_breaker_open = False

        logger.info(f"DataManagerClient initialized with base_url={base_url}")

    async def close(self):
        """Close the HTTP client and cleanup resources."""
        await self._client.aclose()
        logger.info("DataManagerClient closed")

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()

    def _check_circuit_breaker(self):
        """Check if circuit breaker is open."""
        if self._circuit_breaker_open:
            raise ClientConnectionError("Circuit breaker is open - too many failures")

    def _record_success(self):
        """Record successful request."""
        self._circuit_breaker_failures = 0
        if self._circuit_breaker_open:
            logger.info("Circuit breaker closed after successful request")
            self._circuit_breaker_open = False

    def _record_failure(self):
        """Record failed request."""
        self._circuit_breaker_failures += 1
        if self._circuit_breaker_failures >= self._circuit_breaker_threshold:
            self._circuit_breaker_open = True
            logger.error("Circuit breaker opened after too many failures")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((httpx.ConnectError, httpx.TimeoutException)),
    )
    async def _request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Make HTTP request with retries and error handling.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE)
            endpoint: API endpoint path
            params: Query parameters
            json_data: JSON body data

        Returns:
            Response JSON data

        Raises:
            APIError: API returned error response
            ConnectionError: Connection to API failed
            TimeoutError: Request timed out
        """
        self._check_circuit_breaker()

        url = urljoin(self.base_url, endpoint)
        headers = {}

        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            logger.debug(f"{method} {url} params={params}")

            response = await self._client.request(
                method=method,
                url=url,
                params=params,
                json=json_data,
                headers=headers,
            )

            # Check for HTTP errors
            if response.status_code >= 400:
                error_detail = response.text
                try:
                    error_json = response.json()
                    error_detail = error_json.get("detail", error_detail)
                except Exception:
                    pass

                self._record_failure()
                raise APIError(
                    f"API error: {error_detail}",
                    status_code=response.status_code,
                    response=response.json() if response.text else None,
                )

            self._record_success()
            return response.json()

        except httpx.ConnectError as e:
            self._record_failure()
            raise ClientConnectionError(f"Failed to connect to {url}: {e}")

        except httpx.TimeoutException as e:
            self._record_failure()
            raise ClientTimeoutError(f"Request to {url} timed out: {e}")

        except Exception as e:
            self._record_failure()
            logger.error(f"Unexpected error in request to {url}: {e}", exc_info=True)
            raise

    # =============================================================================
    # GENERIC CRUD OPERATIONS
    # =============================================================================

    async def query(
        self,
        database: str,
        collection: str,
        filter: dict[str, Any] | None = None,
        sort: dict[str, int] | None = None,
        limit: int = 100,
        offset: int = 0,
        fields: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Query records from a database collection.

        Args:
            database: Database name ('mysql' or 'mongodb')
            collection: Collection/table name
            filter: Query filter conditions
            sort: Sort specification (field: 1 for asc, -1 for desc)
            limit: Maximum records to return
            offset: Number of records to skip
            fields: Fields to include in response

        Returns:
            API response with data, pagination, and metadata
        """
        params = {
            "limit": limit,
            "offset": offset,
        }

        if filter:
            params["filter"] = json.dumps(filter)
        if sort:
            params["sort"] = json.dumps(sort)
        if fields:
            params["fields"] = ",".join(fields)

        return await self._request(
            "GET",
            f"/api/v1/{database}/{collection}",
            params=params,
        )

    async def insert(
        self,
        database: str,
        collection: str,
        data: Union[dict[str, Any], list[dict[str, Any]]],
        schema: str | None = None,
        validate: bool = False,
    ) -> dict[str, Any]:
        """
        Insert records into a database collection.

        Args:
            database: Database name ('mysql' or 'mongodb')
            collection: Collection/table name
            data: Data to insert (single record or list)
            schema: Schema name for validation
            validate: Enable schema validation

        Returns:
            API response with inserted count
        """
        params = {}
        if schema:
            params["schema"] = schema
        if validate:
            params["validate"] = "true"

        return await self._request(
            "POST",
            f"/api/v1/{database}/{collection}",
            params=params,
            json_data={"data": data},
        )

    async def update(
        self,
        database: str,
        collection: str,
        filter: dict[str, Any],
        data: dict[str, Any],
        upsert: bool = False,
        schema: str | None = None,
        validate: bool = False,
    ) -> dict[str, Any]:
        """
        Update records in a database collection.

        Args:
            database: Database name ('mysql' or 'mongodb')
            collection: Collection/table name
            filter: Filter to identify records to update
            data: Data to update
            upsert: Create record if not found
            schema: Schema name for validation
            validate: Enable schema validation

        Returns:
            API response with updated count
        """
        params = {}
        if schema:
            params["schema"] = schema
        if validate:
            params["validate"] = "true"

        return await self._request(
            "PUT",
            f"/api/v1/{database}/{collection}",
            params=params,
            json_data={
                "filter": filter,
                "data": data,
                "upsert": upsert,
            },
        )

    async def delete(
        self,
        database: str,
        collection: str,
        filter: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Delete records from a database collection.

        Args:
            database: Database name ('mysql' or 'mongodb')
            collection: Collection/table name
            filter: Filter to identify records to delete

        Returns:
            API response with deleted count
        """
        return await self._request(
            "DELETE",
            f"/api/v1/{database}/{collection}",
            json_data={"filter": filter},
        )

    async def batch(
        self,
        database: str,
        collection: str,
        operations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """
        Perform batch operations on a database collection.

        Args:
            database: Database name ('mysql' or 'mongodb')
            collection: Collection/table name
            operations: List of operations (insert, update, delete)

        Returns:
            API response with operation results
        """
        return await self._request(
            "POST",
            f"/api/v1/{database}/{collection}/batch",
            json_data={"operations": operations},
        )

    # =============================================================================
    # DOMAIN-SPECIFIC MARKET DATA OPERATIONS
    # =============================================================================

    async def get_candles(
        self,
        pair: str,
        period: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
        sort_order: str = "asc",
    ) -> dict[str, Any]:
        """
        Get OHLCV candle data for a trading pair.

        Args:
            pair: Trading pair symbol (e.g., 'BTCUSDT')
            period: Candle period (e.g., '1m', '15m', '1h')
            start: Start timestamp
            end: End timestamp
            limit: Maximum number of candles
            offset: Pagination offset
            sort_order: Sort order ('asc' or 'desc')

        Returns:
            API response with candle data
        """
        params = {
            "pair": pair,
            "period": period,
            "limit": limit,
            "offset": offset,
            "sort_order": sort_order,
        }

        if start:
            params["start"] = start.isoformat()
        if end:
            params["end"] = end.isoformat()

        return await self._request("GET", "/data/candles", params=params)

    async def get_trades(
        self,
        pair: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
        sort_order: str = "asc",
    ) -> dict[str, Any]:
        """
        Get individual trade data for a trading pair.

        Args:
            pair: Trading pair symbol
            start: Start timestamp
            end: End timestamp
            limit: Maximum number of trades
            offset: Pagination offset
            sort_order: Sort order ('asc' or 'desc')

        Returns:
            API response with trade data
        """
        params = {
            "pair": pair,
            "limit": limit,
            "offset": offset,
            "sort_order": sort_order,
        }

        if start:
            params["start"] = start.isoformat()
        if end:
            params["end"] = end.isoformat()

        return await self._request("GET", "/data/trades", params=params)

    async def get_funding(
        self,
        pair: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
        sort_order: str = "asc",
    ) -> dict[str, Any]:
        """
        Get funding rate data for a futures trading pair.

        Args:
            pair: Trading pair symbol
            start: Start timestamp
            end: End timestamp
            limit: Maximum number of records
            offset: Pagination offset
            sort_order: Sort order ('asc' or 'desc')

        Returns:
            API response with funding rate data
        """
        params = {
            "pair": pair,
            "limit": limit,
            "offset": offset,
            "sort_order": sort_order,
        }

        if start:
            params["start"] = start.isoformat()
        if end:
            params["end"] = end.isoformat()

        return await self._request("GET", "/data/funding", params=params)

    async def get_depth(self, pair: str) -> dict[str, Any]:
        """
        Get current order book depth for a trading pair.

        Args:
            pair: Trading pair symbol

        Returns:
            API response with order book depth
        """
        return await self._request("GET", "/data/depth", params={"pair": pair})

    # =============================================================================
    # HEALTH AND MONITORING
    # =============================================================================

    async def health(self) -> dict[str, Any]:
        """
        Check Data Manager API health status.

        Returns:
            Health status information
        """
        return await self._request("GET", "/health/readiness")

    async def get_metrics(self) -> dict[str, Any]:
        """
        Get Prometheus metrics from Data Manager.

        Returns:
            Metrics data
        """
        return await self._request("GET", "/metrics")
