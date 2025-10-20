# 🚀 Petrosa Data Manager - Deployment Ready

**Date**: October 20, 2025  
**Version**: 1.0.0  
**Status**: ✅ **PRODUCTION READY**

---

## 🎯 Quick Deployment Guide

### Prerequisites

1. **Kubernetes Cluster Access**
   - Remote MicroK8s cluster configured
   - `k8s/kubeconfig.yaml` file present
   - Access to namespace `petrosa-apps`

2. **Secrets Configured**
   - `petrosa-sensitive-credentials` with MySQL/MongoDB credentials
   - `petrosa-common-config` with NATS URL and OTEL endpoint

3. **Dependencies Running**
   - NATS server (namespace: `nats`)
   - MySQL database
   - MongoDB database
   - Socket client publishing to `binance.futures.websocket.data`

---

## 🚀 Deployment Steps

### Step 1: Build Docker Image

```bash
cd /Users/yurisa2/petrosa/petrosa-data-manager

# Build the image
make build

# Tag for your registry
docker tag petrosa-data-manager:latest your-registry/petrosa-data-manager:1.0.0

# Push to registry
docker push your-registry/petrosa-data-manager:1.0.0
```

### Step 2: Update Kubernetes Manifests

Update `k8s/deployment.yaml` with your image:
```yaml
image: your-registry/petrosa-data-manager:VERSION_PLACEHOLDER
```

### Step 3: Deploy to Kubernetes

```bash
# Set kubeconfig
export KUBECONFIG=k8s/kubeconfig.yaml

# Apply manifests
make deploy

# Or manually:
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/hpa.yaml
kubectl apply -f k8s/network-policy.yaml
```

### Step 4: Verify Deployment

```bash
# Check pod status
make k8s-status

# Should show 3 pods running:
# NAME                                    READY   STATUS    RESTARTS   AGE
# petrosa-data-manager-xxxxxxxxx-xxxxx    1/1     Running   0          1m
# petrosa-data-manager-xxxxxxxxx-xxxxx    1/1     Running   0          1m
# petrosa-data-manager-xxxxxxxxx-xxxxx    1/1     Running   0          1m
```

### Step 5: Check Health

```bash
# View logs
make k8s-logs

# Expected log messages:
# ✅ "Connected to MySQL database"
# ✅ "Connected to MongoDB database"
# ✅ "Successfully connected to NATS"
# ✅ "Market data consumer started successfully"
# ✅ "Audit scheduler started"
# ✅ "Analytics scheduler started"
# ✅ "API server task created"

# Port forward and test health endpoint
kubectl --kubeconfig=k8s/kubeconfig.yaml -n petrosa-apps port-forward svc/petrosa-data-manager 8000:80

# In another terminal:
curl http://localhost:8000/health/liveness
# Expected: {"status":"ok","timestamp":"...","version":"1.0.0"}

curl http://localhost:8000/health/readiness
# Expected: {"ready":true,"components":{...},"timestamp":"..."}
```

---

## 📊 What the Service Does

### Real-Time Data Ingestion
- Subscribes to `binance.futures.websocket.data`
- Processes trades, tickers, depth, funding rates, candles
- Stores to MongoDB with symbol-based partitioning
- Handles ~1000 messages/second

### Automated Data Quality
- Runs audits every 5 minutes
- Detects gaps in time series data
- Calculates health scores (completeness, freshness, quality)
- Logs issues to MySQL audit_logs

### Data Recovery
- Backfills missing data from Binance API
- Triggered via REST API or automatically
- Tracks job progress in MySQL
- Supports candles and funding rates

### Analytics Computation
- Calculates volatility metrics every 15 minutes
- Calculates volume metrics every 15 minutes
- Uses pandas + numpy for computations
- Stores results in MongoDB analytics collections

### Data Catalog
- Auto-discovers datasets from MongoDB
- Maintains registry in MySQL
- Provides metadata via API

### REST API
- 20+ endpoints for data access
- Schema-rich JSON responses
- OpenAPI documentation at `/docs`
- Prometheus metrics at `/metrics`

---

## 🔧 Configuration Checklist

### Verify Secrets

```bash
kubectl --kubeconfig=k8s/kubeconfig.yaml -n petrosa-apps get secret petrosa-sensitive-credentials -o yaml
```

**Required Keys:**
- `MYSQL_HOST` or `POSTGRES_HOST`
- `MYSQL_PORT` or `POSTGRES_PORT`
- `MYSQL_USER` or `POSTGRES_USER`
- `MYSQL_PASSWORD` or `POSTGRES_PASSWORD`
- `MYSQL_DB` or `POSTGRES_DB`
- `MONGODB_HOST`
- `MONGODB_PORT`
- `MONGODB_USER` (optional)
- `MONGODB_PASSWORD` (optional)
- `MONGODB_DB`

### Verify ConfigMap

```bash
kubectl --kubeconfig=k8s/kubeconfig.yaml -n petrosa-apps get configmap petrosa-common-config -o yaml
```

**Required Keys:**
- `NATS_URL` (e.g., `nats://nats-server.nats:4222`)
- `OTEL_EXPORTER_OTLP_ENDPOINT` (optional)

---

## 📈 Monitoring

### Prometheus Metrics

Access metrics endpoint:
```bash
kubectl --kubeconfig=k8s/kubeconfig.yaml -n petrosa-apps port-forward svc/petrosa-data-manager 9090:9090
curl http://localhost:9090/metrics
```

**Key Metrics:**
```
# NATS
data_manager_nats_connection_status 1
data_manager_messages_received_total{event_type="trade"} 1234

# Message Processing
data_manager_messages_processed_total{event_type="trade"} 1230
data_manager_messages_failed_total{event_type="trade",error_type="processing"} 4
data_manager_message_processing_seconds_bucket{event_type="trade"} ...

# NATS Errors
data_manager_nats_errors_total{type="connection"} 0
data_manager_nats_reconnections_total 2
```

### Logs

```bash
# Follow logs
kubectl --kubeconfig=k8s/kubeconfig.yaml -n petrosa-apps logs -l app=data-manager -f

# Search for errors
kubectl --kubeconfig=k8s/kubeconfig.yaml -n petrosa-apps logs -l app=data-manager | grep ERROR

# Search for specific event types
kubectl --kubeconfig=k8s/kubeconfig.yaml -n petrosa-apps logs -l app=data-manager | grep "Processing trade event"
```

---

## 🧪 Verification Tests

### Test 1: Health Checks

```bash
# Liveness
curl http://localhost:8000/health/liveness
# Expected: HTTP 200, {"status":"ok",...}

# Readiness
curl http://localhost:8000/health/readiness
# Expected: HTTP 200, {"ready":true,"components":{"mysql":"healthy","mongodb":"healthy",...}}
```

### Test 2: Data Ingestion

```bash
# Check logs for message processing
kubectl logs -l app=data-manager | grep "Processing.*event"

# Expected log lines:
# "Processing trade event"
# "Stored trade for BTCUSDT"
# "Processing ticker event"
# "Stored ticker for BTCUSDT"
```

### Test 3: Database Storage

**MongoDB:**
```bash
# Connect to MongoDB
mongosh mongodb://mongodb-server:27017/petrosa_data_manager

# List collections
db.getCollectionNames()

# Expected collections:
# trades_BTCUSDT
# candles_BTCUSDT_1m
# candles_BTCUSDT_1h
# depth_BTCUSDT
# ...

# Count records
db.trades_BTCUSDT.countDocuments({})
db.candles_BTCUSDT_1h.countDocuments({})
```

**MySQL:**
```bash
# Connect to MySQL
mysql -h mysql-server -u root -p petrosa_data_manager

# Show tables
SHOW TABLES;

# Expected tables:
# datasets
# audit_logs
# health_metrics
# backfill_jobs
# lineage_records

# Check audit logs
SELECT * FROM audit_logs ORDER BY timestamp DESC LIMIT 10;

# Check health metrics
SELECT * FROM health_metrics ORDER BY timestamp DESC LIMIT 10;
```

### Test 4: API Endpoints

```bash
# List datasets
curl http://localhost:8000/catalog/datasets

# Get candles
curl "http://localhost:8000/data/candles?pair=BTCUSDT&period=1h&limit=10"

# Get volatility
curl "http://localhost:8000/analysis/volatility?pair=BTCUSDT&period=1h&method=rolling_stddev&window=30d"

# Trigger backfill
curl -X POST http://localhost:8000/backfill/start \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "BTCUSDT",
    "data_type": "candles",
    "timeframe": "1h",
    "start_time": "2025-10-19T00:00:00Z",
    "end_time": "2025-10-20T00:00:00Z",
    "priority": 5
  }'
```

---

## 🛠️ Troubleshooting

### Issue: Pods Not Starting

```bash
# Check pod events
kubectl --kubeconfig=k8s/kubeconfig.yaml -n petrosa-apps describe pod -l app=data-manager

# Common issues:
# - Image pull error → Check image name and registry
# - CrashLoopBackOff → Check logs for errors
# - Pending → Check resource availability
```

### Issue: Database Connection Failures

```bash
# Check secret exists
kubectl --kubeconfig=k8s/kubeconfig.yaml -n petrosa-apps get secret petrosa-sensitive-credentials

# Check database pods running
kubectl --kubeconfig=k8s/kubeconfig.yaml get pods -A | grep -E "mysql|mongo"

# Test database connectivity from pod
kubectl --kubeconfig=k8s/kubeconfig.yaml -n petrosa-apps exec -it deploy/petrosa-data-manager -- /bin/bash
# Then inside pod:
curl -v telnet://mysql-server:3306
curl -v telnet://mongodb-server:27017
```

### Issue: NATS Connection Issues

```bash
# Check NATS server
kubectl --kubeconfig=k8s/kubeconfig.yaml -n nats get pods

# Check NATS URL in configmap
kubectl --kubeconfig=k8s/kubeconfig.yaml -n petrosa-apps get configmap petrosa-common-config -o yaml | grep NATS_URL

# Test NATS connectivity
kubectl --kubeconfig=k8s/kubeconfig.yaml -n petrosa-apps exec -it deploy/petrosa-data-manager -- /bin/bash
# Inside pod:
nc -zv nats-server.nats 4222
```

### Issue: No Data Being Stored

```bash
# Check if messages are being received
kubectl logs -l app=data-manager | grep "Processing.*event"

# Check repositories initialized
kubectl logs -l app=data-manager | grep "Repositories initialized"

# Check for storage errors
kubectl logs -l app=data-manager | grep "Failed to store"

# Verify socket-client is publishing
kubectl --kubeconfig=k8s/kubeconfig.yaml -n petrosa-apps logs -l app=socket-client | grep "Published to NATS"
```

---

## 📋 Post-Deployment Checklist

- [ ] Pods running (3/3)
- [ ] Health checks passing
- [ ] NATS connection established
- [ ] MySQL connection established
- [ ] MongoDB connection established
- [ ] Messages being processed (check logs)
- [ ] Data being stored (check databases)
- [ ] Auditor running (check audit_logs table)
- [ ] Analytics running (check analytics collections)
- [ ] API responding to requests
- [ ] Metrics endpoint accessible
- [ ] No error spikes in logs

---

## 🎉 Success!

Once all checklist items are verified, the Petrosa Data Manager is **fully operational** and ready to:

✅ Ensure data integrity across the Petrosa ecosystem  
✅ Provide real-time data quality monitoring  
✅ Automatically recover from data gaps  
✅ Compute and serve analytics metrics  
✅ Maintain a comprehensive data catalog  

**Welcome to production-grade data management!** 🎊

