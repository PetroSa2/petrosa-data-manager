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
* `GET /health?pair={pair}&period={period}` - Data quality metrics

### Data Access

* `GET /data/candles?pair={pair}&period={period}` - OHLCV candle data
* `GET /data/trades?pair={pair}` - Individual trade data
* `GET /data/depth?pair={pair}` - Order book depth
* `GET /data/funding?pair={pair}` - Funding rate data

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

## 📊 Metrics

Prometheus metrics are exposed on port 9090:

* `data_manager_messages_received_total` - Total messages received from NATS
* `data_manager_messages_processed_total` - Successfully processed messages
* `data_manager_messages_failed_total` - Failed messages
* `data_manager_message_processing_seconds` - Message processing time
* `data_manager_nats_connection_status` - NATS connection status

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

## 🎯 Roadmap

* ✅ NATS consumer for market data events
* ✅ FastAPI serving layer with schema-rich endpoints
* ✅ Kubernetes manifests and deployment
* 🚧 Database integration (PostgreSQL + MongoDB)
* 🚧 Auditor implementation (gap detection, health scoring)
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

