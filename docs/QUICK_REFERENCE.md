# Quick Reference Guide

## Common Commands

### Development

```bash
# Setup environment
make setup

# Run locally
make run

# Format code
make format

# Run linters
make lint

# Run tests
make test
```

### Docker

```bash
# Build image
make build

# Run in Docker
make run-docker

# Clean Docker
make docker-clean
```

### Kubernetes

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

## API Quick Reference

### Health Endpoints

```bash
# Liveness probe
curl http://localhost:8000/health/liveness

# Readiness probe
curl http://localhost:8000/health/readiness

# Health summary
curl http://localhost:8000/health/summary

# Data quality
curl "http://localhost:8000/health?pair=BTCUSDT&period=1h"
```

### Data Endpoints

```bash
# Get candles
curl "http://localhost:8000/data/candles?pair=BTCUSDT&period=1h&limit=100"

# Get trades
curl "http://localhost:8000/data/trades?pair=BTCUSDT&limit=100"

# Get order book depth
curl "http://localhost:8000/data/depth?pair=BTCUSDT"

# Get funding rates
curl "http://localhost:8000/data/funding?pair=BTCUSDT&limit=100"
```

### Analytics Endpoints

```bash
# Volatility metrics
curl "http://localhost:8000/analysis/volatility?pair=BTCUSDT&period=1h&method=rolling_stddev&window=30d"

# Volume metrics
curl "http://localhost:8000/analysis/volume?pair=BTCUSDT&period=1h&window=24h"

# Spread metrics
curl "http://localhost:8000/analysis/spread?pair=BTCUSDT"

# Trend metrics
curl "http://localhost:8000/analysis/trend?pair=BTCUSDT&period=1h&window=20"

# Correlation matrix
curl "http://localhost:8000/analysis/correlation?pairs=BTCUSDT,ETHUSDT&period=1h&window=30d"
```

### Catalog Endpoints

```bash
# List datasets
curl http://localhost:8000/catalog/datasets

# Get dataset metadata
curl http://localhost:8000/catalog/datasets/candles_1h_btcusdt

# Get schema
curl http://localhost:8000/catalog/schemas/candles_1h_btcusdt

# Get lineage
curl http://localhost:8000/catalog/lineage/candles_1h_btcusdt
```

### Backfill Endpoints

```bash
# Start backfill job
curl -X POST http://localhost:8000/backfill/start \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "BTCUSDT",
    "data_type": "candles",
    "timeframe": "1h",
    "start_time": "2025-10-01T00:00:00Z",
    "end_time": "2025-10-20T00:00:00Z",
    "priority": 5
  }'

# List backfill jobs
curl http://localhost:8000/backfill/jobs

# Get job status
curl http://localhost:8000/backfill/jobs/{job_id}
```

## Environment Variables

### Core Settings

```bash
ENVIRONMENT=production
LOG_LEVEL=INFO
NATS_URL=nats://nats-server.nats:4222
NATS_CONSUMER_SUBJECT=binance.futures.websocket.data
```

### Database Settings

```bash
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_USER=postgres
POSTGRES_PASSWORD=your_password
POSTGRES_DB=petrosa_data_manager

MONGODB_HOST=mongodb
MONGODB_PORT=27017
MONGODB_DB=petrosa_data_manager
```

### Feature Flags

```bash
ENABLE_AUDITOR=true
ENABLE_BACKFILLER=true
ENABLE_ANALYTICS=true
ENABLE_API=true
```

## Kubernetes Resources

### Check Deployment

```bash
kubectl --kubeconfig=k8s/kubeconfig.yaml -n petrosa-apps get deployments
kubectl --kubeconfig=k8s/kubeconfig.yaml -n petrosa-apps get pods
kubectl --kubeconfig=k8s/kubeconfig.yaml -n petrosa-apps get services
```

### View Logs

```bash
kubectl --kubeconfig=k8s/kubeconfig.yaml -n petrosa-apps logs -l app=data-manager --tail=100 -f
```

### Port Forward

```bash
kubectl --kubeconfig=k8s/kubeconfig.yaml -n petrosa-apps port-forward svc/petrosa-data-manager 8000:80
```

## Troubleshooting

### Check NATS Connection

```bash
# View consumer logs
kubectl --kubeconfig=k8s/kubeconfig.yaml -n petrosa-apps logs -l app=data-manager | grep -i nats

# Check NATS pods
kubectl --kubeconfig=k8s/kubeconfig.yaml -n nats get pods
```

### Check Database Connections

```bash
# Test PostgreSQL
psql -h $POSTGRES_HOST -U $POSTGRES_USER -d $POSTGRES_DB

# Test MongoDB
mongosh mongodb://$MONGODB_HOST:$MONGODB_PORT/$MONGODB_DB
```

### View Metrics

```bash
# Prometheus metrics
curl http://localhost:9090/metrics

# Or via Kubernetes
kubectl --kubeconfig=k8s/kubeconfig.yaml -n petrosa-apps port-forward svc/petrosa-data-manager 9090:9090
curl http://localhost:9090/metrics
```

