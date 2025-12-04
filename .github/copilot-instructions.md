# GitHub Copilot Instructions - Data Manager

## Service Context

**Purpose**: Data aggregation service providing unified API access to market data and system metrics.

**Deployment**: Kubernetes Deployment with leader election (singleton for data sync)

**Role in Ecosystem**: Aggregates MySQL + MongoDB → FastAPI → External clients/dashboards

---

## Architecture

**Data Flow**:
```
MySQL (historical klines) ←┐
MongoDB (config, audit)   ←┼→ Data Manager FastAPI → External Clients
Prometheus (metrics)      ←┘
```

**Key Components**:
- `data_manager/api/` - FastAPI endpoints
- `data_manager/services/` - Data aggregation logic
- `data_manager/db/` - Dual database connections (PostgreSQL + MongoDB)
- `data_manager/cache/` - Redis caching layer

---

## Service-Specific Patterns

### Dual Database Pattern

```python
# ✅ GOOD - Appropriate database for each data type
class DataManager:
    def __init__(self):
        self.pg_client = PostgreSQLClient()  # Time-series data
        self.mongo_client = MongoClient()     # Configuration, audit
    
    async def get_klines(self, symbol, interval):
        # PostgreSQL for historical data
        return await self.pg_client.query(...)
    
    async def get_config(self):
        # MongoDB for configuration
        return await self.mongo_client.find_one(...)
```

### Leader Election

```python
# ✅ Data sync operations require leader
if await leader_elector.is_leader():
    await sync_data_from_sources()
else:
    logger.debug("Not leader, skipping sync")
```

### API Patterns

```python
# ✅ GOOD - Pagination for large datasets
@app.get("/api/v1/klines")
async def get_klines(
    symbol: str,
    interval: str,
    start_time: int,
    limit: int = 1000,  # Default limit
    offset: int = 0
):
    return await query_with_pagination(...)

# ✅ Caching for frequently accessed data
@cached(ttl=60)
async def get_symbols():
    return await db.get_all_symbols()
```

---

## Testing Patterns

```python
# Mock dual databases
@pytest.fixture
def mock_databases():
    with patch('data_manager.db.PostgreSQLClient') as pg_mock, \
         patch('data_manager.db.MongoClient') as mongo_mock:
        yield pg_mock, mongo_mock

# Test API endpoints
def test_get_klines_pagination():
    response = client.get("/api/v1/klines?symbol=BTCUSDT&limit=100&offset=0")
    assert len(response.json()["data"]) <= 100
```

---

## Common Issues

**Slow Queries**: Add database indexes, use caching  
**Connection Pool Exhaustion**: Monitor pg/mongo connection pools  
**Leader Election Split**: Check MongoDB connectivity

---

**Master Rules**: See `.cursorrules` in `petrosa_k8s` repo  
**Service Rules**: `.cursorrules` in this repo

