# Petrosa Data Manager Client

A Python client library for interacting with the Petrosa Data Manager API. Provides both generic CRUD operations and domain-specific market data endpoints with connection pooling, retries, and circuit breaker protection.

## Installation

```bash
# Install from local development
pip install -e .

# Install with development dependencies
pip install -e .[dev]

# Install with test dependencies
pip install -e .[test]
```

## Quick Start

```python
import asyncio
from data_manager_client import DataManagerClient

async def main():
    # Initialize client
    client = DataManagerClient(
        base_url="http://petrosa-data-manager:8000",
        timeout=30,
        max_retries=3
    )
    
    try:
        # Get candle data
        candles = await client.get_candles(
            pair="BTCUSDT",
            period="15m",
            limit=200
        )
        print(f"Retrieved {len(candles['data'])} candles")
        
        # Insert trade data
        result = await client.insert(
            database="mongodb",
            collection="trades_BTCUSDT",
            data={
                "timestamp": "2024-01-01T00:00:00Z",
                "price": "50000.00",
                "quantity": "0.001",
                "side": "buy"
            }
        )
        print(f"Inserted {result['inserted_count']} records")
        
    finally:
        await client.close()

# Run the example
asyncio.run(main())
```

## Generic CRUD Operations

### Query Records
```python
# Query with filters and pagination
records = await client.query(
    database="mongodb",
    collection="candles_BTCUSDT_15m",
    filter={"symbol": "BTCUSDT"},
    sort={"timestamp": -1},
    limit=100,
    offset=0
)
```

### Insert Records
```python
# Single record
result = await client.insert(
    database="mongodb",
    collection="trades_BTCUSDT",
    data={
        "timestamp": "2024-01-01T00:00:00Z",
        "price": "50000.00",
        "quantity": "0.001"
    }
)

# Multiple records
result = await client.insert(
    database="mongodb",
    collection="trades_BTCUSDT",
    data=[
        {"timestamp": "2024-01-01T00:00:00Z", "price": "50000.00"},
        {"timestamp": "2024-01-01T00:01:00Z", "price": "50010.00"}
    ]
)
```

### Update Records
```python
result = await client.update(
    database="mongodb",
    collection="trades_BTCUSDT",
    filter={"trade_id": 12345},
    data={"status": "filled"},
    upsert=False
)
```

### Delete Records
```python
result = await client.delete(
    database="mongodb",
    collection="trades_BTCUSDT",
    filter={"timestamp": {"$lt": "2024-01-01T00:00:00Z"}}
)
```

### Batch Operations
```python
operations = [
    {
        "type": "insert",
        "data": {"symbol": "BTCUSDT", "price": "50000.00"}
    },
    {
        "type": "update",
        "filter": {"symbol": "BTCUSDT"},
        "data": {"updated": True}
    },
    {
        "type": "delete",
        "filter": {"symbol": "ETHUSDT"}
    }
]

result = await client.batch(
    database="mongodb",
    collection="trades",
    operations=operations
)
```

## Domain-Specific Market Data

### Get Candles
```python
candles = await client.get_candles(
    pair="BTCUSDT",
    period="15m",
    start=datetime(2024, 1, 1),
    end=datetime(2024, 1, 2),
    limit=100
)
```

### Get Trades
```python
trades = await client.get_trades(
    pair="BTCUSDT",
    start=datetime(2024, 1, 1),
    end=datetime(2024, 1, 1, 1),  # 1 hour
    limit=1000
)
```

### Get Funding Rates
```python
funding = await client.get_funding(
    pair="BTCUSDT",
    start=datetime(2024, 1, 1),
    end=datetime(2024, 1, 7),  # 1 week
    limit=100
)
```

### Get Order Book Depth
```python
depth = await client.get_depth(pair="BTCUSDT")
print(f"Best bid: {depth['data']['bids'][0]}")
print(f"Best ask: {depth['data']['asks'][0]}")
```

## Error Handling

```python
from data_manager_client import (
    DataManagerClient,
    APIError,
    ConnectionError,
    TimeoutError,
    ValidationError
)

try:
    result = await client.get_candles("BTCUSDT", "15m")
except APIError as e:
    print(f"API Error: {e.message} (Status: {e.status_code})")
except ConnectionError as e:
    print(f"Connection failed: {e}")
except TimeoutError as e:
    print(f"Request timed out: {e}")
except ValidationError as e:
    print(f"Data validation failed: {e}")
```

## Configuration

### Environment Variables
```bash
DATA_MANAGER_URL=http://petrosa-data-manager:8000
DATA_MANAGER_TIMEOUT=30
DATA_MANAGER_MAX_RETRIES=3
DATA_MANAGER_API_KEY=optional-api-key
```

### Client Configuration
```python
client = DataManagerClient(
    base_url=os.getenv("DATA_MANAGER_URL", "http://localhost:8000"),
    timeout=int(os.getenv("DATA_MANAGER_TIMEOUT", "30")),
    max_retries=int(os.getenv("DATA_MANAGER_MAX_RETRIES", "3")),
    pool_size=10,
    api_key=os.getenv("DATA_MANAGER_API_KEY")
)
```

## Advanced Features

### Connection Pooling
The client automatically manages HTTP connection pooling for optimal performance:

```python
client = DataManagerClient(
    base_url="http://data-manager:8000",
    pool_size=20,  # Max 20 keep-alive connections
    timeout=30
)
```

### Circuit Breaker
Automatic circuit breaker protection prevents cascade failures:

```python
# Circuit breaker opens after 5 consecutive failures
# Automatically closes after successful requests
client = DataManagerClient(
    base_url="http://data-manager:8000",
    max_retries=3  # Retry failed requests up to 3 times
)
```

### Async Context Manager
```python
async with DataManagerClient(base_url="http://data-manager:8000") as client:
    candles = await client.get_candles("BTCUSDT", "15m")
    # Client automatically closed when exiting context
```

## Health Monitoring

```python
# Check API health
health = await client.health()
print(f"API Status: {health['status']}")

# Get metrics
metrics = await client.get_metrics()
print(f"Request count: {metrics['requests_total']}")
```

## Type Hints

The client provides full type hints for better IDE support:

```python
from typing import List, Dict, Any
from datetime import datetime

async def process_candles(pair: str) -> List[Dict[str, Any]]:
    client = DataManagerClient(base_url="http://data-manager:8000")
    
    response = await client.get_candles(
        pair=pair,
        period="15m",
        limit=100
    )
    
    return response["data"]
```

## Testing

```python
import pytest
from httpx_mock import HTTPXMock
from data_manager_client import DataManagerClient

@pytest.mark.asyncio
async def test_get_candles():
    with HTTPXMock() as httpx_mock:
        httpx_mock.add_response(
            url="http://data-manager:8000/data/candles",
            json={"data": [{"timestamp": "2024-01-01T00:00:00Z", "close": "50000"}]}
        )
        
        client = DataManagerClient(base_url="http://data-manager:8000")
        result = await client.get_candles("BTCUSDT", "15m")
        
        assert len(result["data"]) == 1
        assert result["data"][0]["close"] == "50000"
```

## License

MIT License - see LICENSE file for details.
