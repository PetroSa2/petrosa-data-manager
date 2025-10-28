# Petrosa Data Manager

**Data integrity, intelligence, and distribution hub for the Petrosa trading ecosystem**

The Data Manager ensures all trading-related datasets remain accurate, consistent, complete, and analyzable. It acts as both a guardian (maintaining data quality) and a gateway (serving structured data and analytics).

---

## 🌐 Overview

The Petrosa Data Manager is responsible for:

* **Data Integrity**: Continuous validation, gap detection, and consistency checking
* **Data Auditing**: Automated health scoring and quality monitoring
* **Data Recovery**: Intelligent backfilling of missing data
* **Analytics Computation**: Market metrics (volatility, volume, spread, trends, correlations)
* **Data Serving**: Schema-rich APIs for downstream consumption
* **Catalog Management**: Dataset registry, schemas, and lineage tracking

---

## 🏗️ Architecture

### Core Components

| Component | Purpose |
|-----------|---------|
| **NATS Consumer** | Subscribe to `binance.futures.websocket.data` for real-time market data |
| **Auditor** | Validate data integrity, detect gaps, duplicates, and anomalies |
| **Backfiller** | Fetch and restore missing data ranges from Binance API |
| **Catalog** | Maintain dataset metadata, schemas, and lineage registry |
| **Analytics Engine** | Compute volatility, volume, spread, deviation, trend, seasonality metrics |
| **API Server** | RESTful endpoints for data access, metrics, health, and catalog |

### Data Flow

```
NATS: binance.futures.websocket.data
  ↓ (subscribe)
Data Manager Consumer
  ↓
Data Validation & Storage (PostgreSQL/MongoDB)
  ↓
Auditor (continuous) → Backfiller (on gaps) → Analytics (scheduled)
  ↓
API Layer (FastAPI) → Downstream consumers (dashboards, strategies, tradeengine)
```

---

## 📚 Documentation Structure

Core documentation (kept up-to-date):
- `README.md` - Project overview and quick start
- `QUICK_REFERENCE.md` - Common commands and workflows
- `DEPLOYMENT_GUIDE.md` - Production deployment
- `docs/MANUAL_DEPLOYMENT_GUIDE.md` - **Manual deployments without code changes**
- `CI_CD_PIPELINE.md` - CI/CD reference
- `TESTING.md` - Testing procedures
- `MAKEFILE.md` - Makefile commands

Archive:
- `docs/archive/` - Historical documentation for reference only
  - `docs/archive/summaries/` - Implementation and feature summaries
  - `docs/archive/fixes/` - Bug fix and resolution reports
  - `docs/archive/investigations/` - Temporary analysis and diagnostic docs
  - `docs/archive/migrations/` - Migration and upgrade documentation

---

## 🚀 Quick Start

### Prerequisites

* Python 3.11+
* Docker
* kubectl (for Kubernetes deployment)
* Access to remote MicroK8s cluster

### Installation

```bash
# Complete setup
make setup

# Or manually
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

### Local Development

```bash
# Run locally
make run

# Or directly
python -m data_manager.main
```

### Docker

```bash
# Build image
make build

# Run in Docker
make run-docker
```

### Kubernetes Deployment

```bash
# Deploy to cluster
make deploy

# Check status
make k8s-status

# View logs
make k8s-logs

# Clean up
make k8s-clean
```

---

## 📡 API Endpoints

### Health & Status

* `GET /health/liveness` - Kubernetes liveness probe
* `GET /health/readiness` - Kubernetes readiness probe
* `GET /health/summary` - Overall system health
* `GET /health/databases` - Detailed database connection status
* `GET /health/connections` - Connection pool statistics
* `GET /health?pair={pair}&period={period}` - Data quality metrics

### Data Access (Domain-Specific)

* `GET /data/candles?pair={pair}&period={period}` - OHLCV candle data
* `GET /data/trades?pair={pair}` - Individual trade data
* `GET /data/depth?pair={pair}` - Order book depth
* `GET /data/funding?pair={pair}` - Funding rate data

### Generic CRUD API

* `GET /api/v1/{database}/{collection}` - Query records with filtering, sorting, pagination
* `POST /api/v1/{database}/{collection}` - Insert single or multiple records
* `PUT /api/v1/{database}/{collection}` - Update records with filtering
* `DELETE /api/v1/{database}/{collection}` - Delete records with filtering
* `POST /api/v1/{database}/{collection}/batch` - Batch operations (insert/update/delete)

### Raw Query API

* `POST /api/v1/raw/mysql` - Execute raw SQL queries (with safety validation)
* `POST /api/v1/raw/mongodb` - Execute raw MongoDB queries/aggregations

### Schema Registry API

* `POST /schemas/{database}/{name}` - Register new schema
* `GET /schemas/{database}/{name}` - Get latest schema
* `GET /schemas/{database}/{name}/versions` - List schema versions
* `GET /schemas/{database}/{name}/versions/{version}` - Get specific version
* `PUT /schemas/{database}/{name}/versions/{version}` - Update schema
* `DELETE /schemas/{database}/{name}/versions/{version}` - Deprecate schema
* `GET /schemas` - List all schemas (both databases)
* `GET /schemas?database={db}` - List schemas for specific database
* `POST /schemas/validate` - Validate data against schema
* `POST /schemas/compatibility` - Check schema compatibility
* `GET /schemas/search?query={query}` - Search schemas by name/description
* `POST /schemas/bootstrap` - Bootstrap common schemas
* `GET /schemas/cache/stats` - Get schema cache statistics
* `POST /schemas/cache/clear` - Clear schema cache

### Analytics

* `GET /analysis/volatility?pair={pair}&period={period}&method={method}` - Volatility metrics
* `GET /analysis/volume?pair={pair}&period={period}` - Volume metrics
* `GET /analysis/spread?pair={pair}` - Spread and liquidity
* `GET /analysis/trend?pair={pair}&period={period}` - Trend indicators
* `GET /analysis/correlation?pairs={pairs}&period={period}` - Correlation matrix

### Catalog

* `GET /catalog/datasets` - List all datasets
* `GET /catalog/datasets/{dataset_id}` - Dataset metadata
* `GET /catalog/schemas/{dataset_id}` - Schema definition
* `GET /catalog/lineage/{dataset_id}` - Data lineage

### Backfill

* `POST /backfill/start` - Trigger manual backfill
* `GET /backfill/jobs` - List backfill jobs
* `GET /backfill/jobs/{job_id}` - Job status

---

## 🚀 API Usage Examples

### Generic CRUD Operations

#### Query Records
```bash
# Get all records from a collection
curl "http://localhost:8000/api/v1/mongodb/candles_BTCUSDT_1m"

# Filter and sort records
curl "http://localhost:8000/api/v1/mongodb/candles_BTCUSDT_1m?filter={\"symbol\":\"BTCUSDT\"}&sort={\"timestamp\":-1}&limit=100"

# Paginate results
curl "http://localhost:8000/api/v1/mongodb/trades_BTCUSDT?limit=50&offset=100"
```

#### Insert Records
```bash
# Insert single record
curl -X POST "http://localhost:8000/api/v1/mongodb/candles_BTCUSDT_1m" \
  -H "Content-Type: application/json" \
  -d '{"data": {"symbol": "BTCUSDT", "open": 50000, "high": 51000, "low": 49000, "close": 50500, "volume": 1000}}'

# Insert multiple records
curl -X POST "http://localhost:8000/api/v1/mongodb/trades_BTCUSDT" \
  -H "Content-Type: application/json" \
  -d '{"data": [{"symbol": "BTCUSDT", "price": 50000, "quantity": 0.1}, {"symbol": "BTCUSDT", "price": 50100, "quantity": 0.2}]}'
```

#### Batch Operations
```bash
# Batch insert/update/delete
curl -X POST "http://localhost:8000/api/v1/mongodb/candles_BTCUSDT_1m/batch" \
  -H "Content-Type: application/json" \
  -d '{
    "operations": [
      {"type": "insert", "data": {"symbol": "BTCUSDT", "open": 50000}},
      {"type": "update", "filter": {"symbol": "BTCUSDT"}, "data": {"updated": true}},
      {"type": "delete", "filter": {"symbol": "ETHUSDT"}}
    ]
  }'
```

### Schema Registry Operations

#### Register Schema
```bash
# Register MongoDB schema for candles
curl -X POST "http://localhost:8000/schemas/mongodb/candle_v1" \
  -H "Content-Type: application/json" \
  -d '{
    "version": 1,
    "schema": {
      "type": "object",
      "required": ["symbol", "timestamp", "open", "high", "low", "close", "volume"],
      "properties": {
        "symbol": {"type": "string", "pattern": "^[A-Z]+$"},
        "timestamp": {"type": "string", "format": "date-time"},
        "open": {"type": "number", "minimum": 0},
        "high": {"type": "number", "minimum": 0},
        "low": {"type": "number", "minimum": 0},
        "close": {"type": "number", "minimum": 0},
        "volume": {"type": "number", "minimum": 0}
      }
    },
    "description": "OHLCV candle data schema"
  }'

# Register MySQL schema for orders
curl -X POST "http://localhost:8000/schemas/mysql/order_v1" \
  -H "Content-Type: application/json" \
  -d '{
    "version": 1,
    "schema": {
      "type": "object",
      "required": ["order_id", "symbol", "side", "type", "status"],
      "properties": {
        "order_id": {"type": "string"},
        "symbol": {"type": "string", "pattern": "^[A-Z]+$"},
        "side": {"type": "string", "enum": ["BUY", "SELL"]},
        "type": {"type": "string", "enum": ["LIMIT", "MARKET"]},
        "status": {"type": "string", "enum": ["NEW", "FILLED", "CANCELED"]},
        "quantity": {"type": "number", "minimum": 0},
        "price": {"type": "number", "minimum": 0}
      }
    },
    "description": "Order management schema"
  }'
```

#### Get Schema Information
```bash
# Get latest schema
curl "http://localhost:8000/schemas/mongodb/candle_v1"

# Get specific version
curl "http://localhost:8000/schemas/mongodb/candle_v1/versions/1"

# List all versions
curl "http://localhost:8000/schemas/mongodb/candle_v1/versions"

# List all schemas
curl "http://localhost:8000/schemas"

# List schemas by database
curl "http://localhost:8000/schemas?database=mongodb"
```

#### Validate Data Against Schema
```bash
# Validate single record
curl -X POST "http://localhost:8000/schemas/validate" \
  -H "Content-Type: application/json" \
  -d '{
    "database": "mongodb",
    "schema_name": "candle_v1",
    "data": {
      "symbol": "BTCUSDT",
      "timestamp": "2025-01-01T00:00:00Z",
      "open": 50000,
      "high": 51000,
      "low": 49000,
      "close": 50500,
      "volume": 100.5
    }
  }'

# Validate batch data
curl -X POST "http://localhost:8000/schemas/validate" \
  -H "Content-Type: application/json" \
  -d '{
    "database": "mongodb",
    "schema_name": "candle_v1",
    "data": [
      {"symbol": "BTCUSDT", "timestamp": "2025-01-01T00:00:00Z", "open": 50000, "high": 51000, "low": 49000, "close": 50500, "volume": 100.5},
      {"symbol": "ETHUSDT", "timestamp": "2025-01-01T00:00:00Z", "open": 3000, "high": 3100, "low": 2900, "close": 3050, "volume": 500.2}
    ]
  }'
```

#### Schema Compatibility Checking
```bash
# Check compatibility between schema versions
curl -X POST "http://localhost:8000/schemas/compatibility" \
  -H "Content-Type: application/json" \
  -d '{
    "database": "mongodb",
    "schema_name": "candle_v1",
    "old_version": 1,
    "new_version": 2
  }'
```

#### Bootstrap Common Schemas
```bash
# Bootstrap all common schemas for MongoDB
curl -X POST "http://localhost:8000/schemas/bootstrap" \
  -H "Content-Type: application/json" \
  -d '{
    "database": "mongodb",
    "schemas": [
      {
        "version": 1,
        "schema": {
          "type": "object",
          "required": ["symbol", "timestamp", "open", "high", "low", "close", "volume"],
          "properties": {
            "symbol": {"type": "string", "pattern": "^[A-Z]+$"},
            "timestamp": {"type": "string", "format": "date-time"},
            "open": {"type": "number", "minimum": 0},
            "high": {"type": "number", "minimum": 0},
            "low": {"type": "number", "minimum": 0},
            "close": {"type": "number", "minimum": 0},
            "volume": {"type": "number", "minimum": 0}
          }
        },
        "description": "OHLCV candle data schema"
      }
    ],
    "overwrite_existing": false
  }'
```

### CRUD Operations with Schema Validation

#### Insert with Validation
```bash
# Insert with automatic schema validation
curl -X POST "http://localhost:8000/api/v1/mongodb/candles_BTCUSDT_1m?schema=candle_v1&validate=true" \
  -H "Content-Type: application/json" \
  -d '{
    "data": {
      "symbol": "BTCUSDT",
      "timestamp": "2025-01-01T00:00:00Z",
      "open": 50000,
      "high": 51000,
      "low": 49000,
      "close": 50500,
      "volume": 100.5
    }
  }'

# Batch insert with validation
curl -X POST "http://localhost:8000/api/v1/mongodb/candles_BTCUSDT_1m?schema=candle_v1&validate=true" \
  -H "Content-Type: application/json" \
  -d '{
    "data": [
      {"symbol": "BTCUSDT", "timestamp": "2025-01-01T00:00:00Z", "open": 50000, "high": 51000, "low": 49000, "close": 50500, "volume": 100.5},
      {"symbol": "BTCUSDT", "timestamp": "2025-01-01T01:00:00Z", "open": 50500, "high": 51500, "low": 49500, "close": 51000, "volume": 150.2}
    ]
  }'
```

#### Update with Validation
```bash
# Update with schema validation
curl -X PUT "http://localhost:8000/api/v1/mysql/orders?schema=order_v1&validate=true" \
  -H "Content-Type: application/json" \
  -d '{
    "filter": {"order_id": "12345"},
    "data": {
      "status": "FILLED",
      "updated_at": "2025-01-01T12:00:00Z"
    }
  }'
```

### Raw Query Operations

#### MySQL Raw Queries
```bash
# Execute SQL query
curl -X POST "http://localhost:8000/api/v1/raw/mysql" \
  -H "Content-Type: application/json" \
  -d '{"query": "SELECT COUNT(*) as total FROM audit_logs WHERE timestamp > NOW() - INTERVAL 1 HOUR"}'
```

#### MongoDB Raw Queries
```bash
# Find query
curl -X POST "http://localhost:8000/api/v1/raw/mongodb" \
  -H "Content-Type: application/json" \
  -d '{"query": "candles_BTCUSDT_1m: {\"find\": {\"symbol\": \"BTCUSDT\"}, \"limit\": 10}"}'

# Aggregation pipeline
curl -X POST "http://localhost:8000/api/v1/raw/mongodb" \
  -H "Content-Type: application/json" \
  -d '{"query": "{\"collection\": \"trades_BTCUSDT\", \"aggregate\": [{\"$group\": {\"_id\": \"$symbol\", \"count\": {\"$sum\": 1}}}]}"}'
```

### Health Monitoring

#### Check Database Health
```bash
# Overall health
curl "http://localhost:8000/health/readiness"

# Detailed database status
curl "http://localhost:8000/health/databases"

# Connection pool statistics
curl "http://localhost:8000/health/connections"
```

### Metrics and Monitoring

#### Prometheus Metrics
```bash
# View all metrics
curl "http://localhost:8000/metrics"

# Key metrics to monitor:
# - data_manager_requests_total (request count by endpoint/status)
# - data_manager_request_duration_seconds (request latency)
# - data_manager_database_operations_total (DB operation count)
# - data_manager_active_connections (connection pool status)
```

---

## 🔧 Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `NATS_URL` | `nats://localhost:4222` | NATS server URL |
| `NATS_CONSUMER_SUBJECT` | `binance.futures.websocket.data` | NATS subject to subscribe |
| `POSTGRES_URL` | - | PostgreSQL connection string |
| `MONGODB_URL` | - | MongoDB connection string |
| `ENABLE_AUDITOR` | `true` | Enable data auditor |
| `ENABLE_BACKFILLER` | `true` | Enable backfiller |
| `ENABLE_ANALYTICS` | `true` | Enable analytics engine |
| `ENABLE_API` | `true` | Enable API server |
| `API_PORT` | `8000` | API server port |
| `AUDIT_INTERVAL` | `300` | Audit interval in seconds |
| `ANALYTICS_INTERVAL` | `900` | Analytics interval in seconds |
| `ENABLE_LEADER_ELECTION` | `true` | Enable MongoDB-based leader election |
| `LEADER_ELECTION_HEARTBEAT_INTERVAL` | `10` | Leader heartbeat interval (seconds) |
| `LEADER_ELECTION_TIMEOUT` | `30` | Leader election timeout (seconds) |
| `ENABLE_AUTO_BACKFILL` | `false` | Enable automatic backfill for detected gaps |
| `MIN_AUTO_BACKFILL_GAP` | `3600` | Minimum gap size to trigger backfill (seconds) |
| `MAX_AUTO_BACKFILL_JOBS` | `5` | Maximum concurrent backfill jobs |
| `ENABLE_DUPLICATE_REMOVAL` | `false` | Enable automatic duplicate removal |
| `DUPLICATE_RESOLUTION_STRATEGY` | `keep_newest` | Duplicate resolution strategy |

### Kubernetes Configuration

The service uses existing shared secrets and configmaps:

* **Secret**: `petrosa-sensitive-credentials` (database credentials)
* **ConfigMap**: `petrosa-common-config` (shared settings)
* **ConfigMap**: `petrosa-data-manager-config` (service-specific)

---

## 🧪 Development

### Code Quality

```bash
# Run linters
make lint

# Format code
make format

# Run tests
make test

# Security scan
make security
```

### Complete Pipeline

```bash
# Run all checks
make pipeline
```

---

## 🗃️ Schema Registry

The Schema Registry provides centralized schema management for all Petrosa services, ensuring data consistency and validation across the platform.

### Key Features

* **Database-Specific Storage**: Schemas stored in their respective databases (MySQL for structured data, MongoDB for time-series)
* **Version Management**: Full schema versioning with compatibility checking
* **Automatic Validation**: CRUD operations can validate data against registered schemas
* **Schema Discovery**: Easy schema exploration and documentation
* **Compatibility Checking**: Validate schema evolution and migration paths
* **Bootstrap Support**: Predefined schemas for common data types

### Schema Storage Strategy

```
┌─────────────────────────────────────────────────────────────┐
│              petrosa-data-manager (API Gateway)              │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Schema Registry REST API                                   │
│  ├─ /schemas?database={db}           (list schemas)        │
│  ├─ /schemas/{db}/{name}             (get/register)        │
│  ├─ /schemas/{db}/{name}/versions    (versions)            │
│  └─ /schemas/validate?database={db}  (validate)            │
│                                                             │
└──────────────────┬───────────────────┬──────────────────────┘
                   │                   │
        ┌──────────▼─────────┐  ┌─────▼──────────────┐
        │      MySQL         │  │     MongoDB        │
        ├────────────────────┤  ├────────────────────┤
        │ schemas table:     │  │ schemas collection:│
        │  - schema_name     │  │  {                 │
        │  - version         │  │    name: "...",    │
        │  - schema_json     │  │    version: 1,     │
        │  - created_at      │  │    schema: {...},  │
        │  - status          │  │    created_at: ... │
        │                    │  │  }                 │
        └────────────────────┘  └────────────────────┘
```

### Common Schemas

#### MongoDB Schemas (Time-Series Data)
* **candle_v1**: OHLCV candle data with volume metrics
* **trade_v1**: Trade execution data with order information
* **depth_v1**: Order book depth with bid/ask arrays
* **funding_v1**: Funding rate data with mark/index prices

#### MySQL Schemas (Structured Data)
* **order_v1**: Order management with status tracking
* **health_metrics_v1**: Data quality metrics and monitoring
* **audit_log_v1**: Data integrity audit logs
* **strategy_signal_v1**: Trading strategy signals and decisions

### Schema Validation Integration

All CRUD operations support optional schema validation:

```bash
# Insert with validation
POST /api/v1/mongodb/candles_BTCUSDT_1m?schema=candle_v1&validate=true

# Update with validation  
PUT /api/v1/mysql/orders?schema=order_v1&validate=true

# Batch operations with validation
POST /api/v1/mongodb/candles_BTCUSDT_1m?schema=candle_v1&validate=true
```

### Configuration

Environment variables for schema registry:

```bash
# Schema validation settings
SCHEMA_VALIDATION_ENABLED=true
SCHEMA_STRICT_MODE=false
SCHEMA_CACHE_TTL=300
SCHEMA_AUTO_REGISTER=false
SCHEMA_MAX_VERSIONS=10
SCHEMA_COMPATIBILITY_MODE=BACKWARD
```

---

## 📊 Metrics

Prometheus metrics are exposed on port 9090:

* `data_manager_messages_received_total` - Total messages received from NATS
* `data_manager_messages_processed_total` - Successfully processed messages
* `data_manager_messages_failed_total` - Failed messages
* `data_manager_message_processing_seconds` - Message processing time
* `data_manager_nats_connection_status` - NATS connection status
* `data_manager_requests_total` - Request count by endpoint/status
* `data_manager_request_duration_seconds` - Request latency histogram
* `data_manager_database_operations_total` - Database operation counts
* `data_manager_active_connections` - Connection pool status
* `data_manager_errors_total` - Error rates by endpoint

---

## 🗂️ Project Structure

```
petrosa-data-manager/
├── data_manager/
│   ├── models/          # Pydantic data models
│   ├── consumer/        # NATS consumer and message handling
│   ├── auditor/         # Data integrity validation
│   ├── backfiller/      # Gap recovery and backfilling
│   ├── catalog/         # Dataset registry and metadata
│   ├── analytics/       # Metrics computation
│   ├── api/             # FastAPI endpoints
│   │   └── routes/      # API route modules
│   └── main.py          # Application entry point
├── k8s/                 # Kubernetes manifests
├── tests/               # Test suite
├── constants.py         # Configuration constants
├── otel_init.py         # OpenTelemetry initialization
├── Dockerfile           # Container image
├── Makefile             # Development commands
└── README.md            # This file
```

---

## 🔗 Integration

### Event Bus (NATS)

The Data Manager subscribes to:

* `binance.futures.websocket.data` - Real-time market data from socket-client

Supported event types:

* `trade` - Individual trades
* `ticker` - 24h ticker statistics
* `depth` - Order book depth updates
* `markPrice` - Mark price updates
* `fundingRate` - Funding rate updates
* `kline` - Candle/kline data

### Databases

* **PostgreSQL**: Metadata, catalog, audit logs, health metrics
* **MongoDB**: Time series data (candles, trades, depth), computed metrics

---

## 🔒 Leader Election

The Data Manager uses MongoDB-based leader election to ensure only one pod runs background schedulers (auditor, analytics) in multi-replica deployments.

### How It Works

1. **Election**: On startup, each pod attempts to become leader via atomic MongoDB write
2. **Heartbeat**: Leader sends heartbeat every 10 seconds to prove it's alive
3. **Failover**: If leader fails (no heartbeat for 30s), followers elect new leader
4. **Safety**: Prevents duplicate work and database contention

### Configuration

```yaml
# Enable leader election (recommended for production)
ENABLE_LEADER_ELECTION: "true"

# Heartbeat frequency
LEADER_ELECTION_HEARTBEAT_INTERVAL: "10"  # seconds

# Leader timeout
LEADER_ELECTION_TIMEOUT: "30"  # seconds
```

### Monitoring

```bash
# Check which pod is the leader
kubectl exec -it data-manager-xxx -- curl localhost:8000/health/leader

# Check audit scheduler status
kubectl exec -it data-manager-xxx -- curl localhost:8000/health/audit-status
```

See [docs/AUDITOR.md](docs/AUDITOR.md) for complete details.

---

## 🎯 Roadmap

* ✅ NATS consumer for market data events
* ✅ FastAPI serving layer with schema-rich endpoints
* ✅ Kubernetes manifests and deployment
* ✅ Auditor implementation with leader election
  * ✅ Gap detection with auto-backfill integration
  * ✅ Duplicate detection and removal
  * ✅ Health scoring with enhanced metrics
  * ✅ MongoDB-based leader election for multi-replica safety
* ✅ Leader election for background schedulers
* 🚧 Database integration (PostgreSQL + MongoDB)
* 🚧 Backfiller implementation (Binance API integration)
* 🚧 Analytics engine (all metric calculators)
* 🚧 Catalog management (dataset registry)
* 🚧 Comprehensive test suite
* 🚧 CI/CD pipeline

---

## 📚 Documentation

* **API Documentation**: Available at `/docs` when running (Swagger UI)
* **Metrics**: Available at `/metrics` (Prometheus format)
* **Health**: Available at `/health/*` endpoints

---

## 🛠️ Troubleshooting

### NATS Connection Issues

```bash
# Check NATS connectivity
kubectl --kubeconfig=k8s/kubeconfig.yaml -n nats get pods

# View logs
make k8s-logs
```

### Database Connection Issues

```bash
# Verify secrets are configured
kubectl --kubeconfig=k8s/kubeconfig.yaml -n petrosa-apps get secret petrosa-sensitive-credentials
```

### API Not Responding

```bash
# Check pod status
make k8s-status

# Check readiness
curl http://petrosa-data-manager.petrosa-apps/health/readiness
```

---

## 📝 License

MIT License - Petrosa Systems

---

## 👥 Authors

Petrosa Systems - Trading Infrastructure Team

