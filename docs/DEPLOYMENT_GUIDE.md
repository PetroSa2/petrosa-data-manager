# Petrosa Data Manager - Deployment Guide

**Version**: 1.1.0  
**Date**: October 20, 2025  
**Status**: ✅ Production Ready

---

## 📋 Deployment Architecture

### Repository Structure

The Petrosa Data Manager follows the centralized deployment pattern:

- **Application Code**: `petrosa-data-manager/` repository
  - Source code, tests, Dockerfile
  - CI pipeline (.github/workflows/ci.yml)
  - CD pipeline (.github/workflows/deploy.yml)

- **Kubernetes Manifests**: `petrosa_k8s/` repository
  - K8s manifests in `k8s/data-manager/`
  - Centralized configuration management
  - Shared secrets and configmaps

---

## 🗂️ Kubernetes Resources Location

### Main Application (petrosa-apps namespace)
Location: `petrosa_k8s/k8s/data-manager/`

Files:
- `configmap.yaml` - Service-specific configuration
- `deployment.yaml` - Application deployment (3-10 replicas)
- `service.yaml` - ClusterIP service
- `hpa.yaml` - Horizontal Pod Autoscaler
- `network-policy.yaml` - Network security policies
- `README.md` - Deployment documentation

### Shared Configuration (petrosa-apps namespace)
Location: `petrosa_k8s/k8s/shared/`

- `configmaps/petrosa-common-config.yaml` - NATS_URL, OTEL endpoint
- `secrets/petrosa-sensitive-credentials.yaml` - Database credentials

---

## 🔐 Required Secrets (Already Configured)

The following secrets are already configured in `petrosa-sensitive-credentials`:

```yaml
# MySQL (External Database)
MYSQL_URI: mysql+pymysql://user:pass@petrosa_crypto.mysql.dbaas.com.br:3306/petrosa_crypto

# MongoDB (External/Atlas)
mongodb-url: mongodb://mongodb-service.petrosa-apps.svc.cluster.local:27017
# or mongodb-connection-string for Atlas

# Binance API (Optional - for backfilling)
BINANCE_API_KEY: <key>
BINANCE_API_SECRET: <secret>
```

**No additional secrets needed!** ✅

---

## 🌐 External Services

### MySQL Database
- **Type**: External (dbaas.com.br)
- **Connection**: Via `MYSQL_URI` from secrets
- **Usage**: Metadata, audit logs, health metrics, backfill jobs, catalog
- **Tables**: Auto-created by adapter on first connection

### MongoDB Database
- **Type**: External (MongoDB Atlas or internal service)
- **Connection**: Via `mongodb-url` from secrets
- **Usage**: Time series data (candles, trades, depth, analytics)
- **Collections**: Auto-created dynamically per symbol

### NATS Server
- **Namespace**: `nats`
- **Connection**: Via `NATS_URL` from `petrosa-common-config`
- **Subject**: `binance.futures.websocket.data`

**All external dependencies already configured!** ✅

---

## 🚀 Deployment Process

### Automatic Deployment (Recommended)

Deployment happens automatically via GitHub Actions when code is pushed to `main`:

```bash
# 1. Commit and push to main
git add .
git commit -m "feat: implement data manager"
git push origin main

# 2. GitHub Actions automatically:
#    ✅ Runs CI (lint, test, security, build)
#    ✅ Creates semantic version tag (v1.0.0, v1.0.1, etc.)
#    ✅ Builds Docker image
#    ✅ Pushes to Docker Hub (yurisa2/petrosa-data-manager)
#    ✅ Checks out petrosa_k8s repository
#    ✅ Updates VERSION_PLACEHOLDER in manifests
#    ✅ Applies manifests to petrosa-apps namespace
#    ✅ Waits for rollout to complete
#    ✅ Verifies deployment

# 3. Check deployment status
kubectl get pods -l app=data-manager -n petrosa-apps --insecure-skip-tls-verify
```

### Manual Deployment

If you need to deploy manually:

```bash
# 1. Build and push Docker image
cd /Users/yurisa2/petrosa/petrosa-data-manager
docker build -t yurisa2/petrosa-data-manager:v1.0.0 .
docker push yurisa2/petrosa-data-manager:v1.0.0

# 2. Update manifests in petrosa_k8s
cd /Users/yurisa2/petrosa/petrosa_k8s
# Update VERSION_PLACEHOLDER to v1.0.0 in k8s/data-manager/*.yaml

# 3. Apply manifests
kubectl apply -f k8s/data-manager/ -n petrosa-apps --insecure-skip-tls-verify

# 4. Check status
kubectl rollout status deployment/petrosa-data-manager -n petrosa-apps --insecure-skip-tls-verify
```

---

## ✅ Pre-Deployment Checklist

- [x] MySQL database accessible (external: dbaas.com.br)
- [x] MongoDB database accessible (external or Atlas)
- [x] NATS server running in `nats` namespace
- [x] `petrosa-sensitive-credentials` secret exists in `petrosa-apps` namespace
- [x] `petrosa-common-config` configmap exists in `petrosa-apps` namespace
- [x] Socket client publishing to `binance.futures.websocket.data`
- [x] Kubernetes manifests created in `petrosa_k8s/k8s/data-manager/`
- [x] GitHub Actions secrets configured (DOCKERHUB_USERNAME, DOCKERHUB_TOKEN, KUBE_CONFIG_DATA)
- [x] Docker Hub repository exists (yurisa2/petrosa-data-manager)

---

## 🔍 Post-Deployment Verification

### 1. Check Pod Status

```bash
kubectl get pods -l app=data-manager -n petrosa-apps --insecure-skip-tls-verify

# Expected: 3 pods in Running state
# NAME                                    READY   STATUS    RESTARTS   AGE
# petrosa-data-manager-xxxxxxxxx-xxxxx    1/1     Running   0          1m
# petrosa-data-manager-xxxxxxxxx-xxxxx    1/1     Running   0          1m
# petrosa-data-manager-xxxxxxxxx-xxxxx    1/1     Running   0          1m
```

### 2. Check Service

```bash
kubectl get svc petrosa-data-manager -n petrosa-apps --insecure-skip-tls-verify

# Expected: ClusterIP service on ports 80, 9090
```

### 3. Check Logs

```bash
kubectl logs -l app=data-manager -n petrosa-apps --tail=100 --insecure-skip-tls-verify

# Expected log messages:
# ✅ "Connected to MySQL database"
# ✅ "Connected to MongoDB database"
# ✅ "Successfully connected to NATS"
# ✅ "Repositories initialized successfully"
# ✅ "Market data consumer started successfully"
# ✅ "Audit scheduler started"
# ✅ "Analytics scheduler started"
```

### 4. Test Health Endpoints

```bash
# Port forward
kubectl port-forward svc/petrosa-data-manager 8000:80 -n petrosa-apps --insecure-skip-tls-verify

# In another terminal:
curl http://localhost:8000/health/liveness
# Expected: {"status":"ok","timestamp":"...","version":"1.0.0"}

curl http://localhost:8000/health/readiness
# Expected: {"ready":true,"components":{"mysql":"healthy","mongodb":"healthy",...}}
```

### 5. Verify Database Connections

```bash
# Check logs for database connection success
kubectl logs -l app=data-manager -n petrosa-apps --insecure-skip-tls-verify | grep -E "Connected to MySQL|Connected to MongoDB"

# Expected:
# Connected to MySQL database
# Connected to MongoDB database: petrosa_data_manager
```

### 6. Verify NATS Consumption

```bash
# Check logs for NATS messages
kubectl logs -l app=data-manager -n petrosa-apps --insecure-skip-tls-verify | grep -E "Processing.*event|Stored.*for"

# Expected:
# Processing trade event
# Stored trade for BTCUSDT
# Processing ticker event
# Stored ticker for BTCUSDT
```

### 7. Check Metrics

```bash
# Port forward metrics
kubectl port-forward svc/petrosa-data-manager 9090:9090 -n petrosa-apps --insecure-skip-tls-verify

# Query metrics
curl http://localhost:9090/metrics | grep data_manager

# Expected metrics:
# data_manager_nats_connection_status 1
# data_manager_messages_received_total{event_type="trade"} X
# data_manager_messages_processed_total{event_type="trade"} X
```

---

## 📊 Deployment Workflow

### GitHub Actions CD Pipeline

**Trigger**: Push to `main` branch

**Jobs**:
1. **create-release** - Generate semantic version (v1.0.0 → v1.0.1)
2. **build-and-push** - Build Docker image, push to Docker Hub
3. **deploy** - Apply K8s manifests from `petrosa_k8s` repository
4. **notify** - Send deployment status notification
5. **cleanup** - Clean up old images

**Secrets Used**:
- `DOCKERHUB_USERNAME` - Docker Hub username
- `DOCKERHUB_TOKEN` - Docker Hub access token
- `KUBE_CONFIG_DATA` - Base64 encoded kubeconfig
- `GITHUB_TOKEN` - For creating tags and accessing petrosa_k8s

---

## 🔧 Configuration

### Environment Variables (from ConfigMap)

```yaml
# Feature Flags
ENABLE_AUDITOR: "true"
ENABLE_BACKFILLER: "true"
ENABLE_ANALYTICS: "true"
ENABLE_API: "true"

# Intervals
AUDIT_INTERVAL: "300"        # 5 minutes
ANALYTICS_INTERVAL: "900"    # 15 minutes

# Processing
MAX_BATCH_SIZE: "1000"
MAX_CONCURRENT_TASKS: "10"
MESSAGE_QUEUE_SIZE: "10000"

# Trading Pairs
SUPPORTED_PAIRS: "BTCUSDT,ETHUSDT,BNBUSDT,ADAUSDT,SOLUSDT"
```

### Database Connections (from Secrets)

```yaml
# MySQL - External dbaas.com.br
MYSQL_URI: mysql+pymysql://petrosa_crypto:***@petrosa_crypto.mysql.dbaas.com.br:3306/petrosa_crypto

# MongoDB - External Atlas or Internal Service
mongodb-url: mongodb://mongodb-service.petrosa-apps.svc.cluster.local:27017
# OR
mongodb-connection-string: mongodb+srv://***@petrosa.***mongodb.net/
```

---

## 🎯 What Gets Deployed

### Namespace: petrosa-apps

**Resources**:
- 1 Deployment (petrosa-data-manager)
- 1 Service (ClusterIP)
- 1 HPA (3-10 replicas)
- 1 ConfigMap (petrosa-data-manager-config)
- 1 NetworkPolicy

**Pods**:
- Min: 3 replicas
- Max: 10 replicas (auto-scales on CPU 70%, Memory 80%)
- Resources per pod: 512Mi-2Gi RAM, 250m-1000m CPU

**Network Access**:
- Ingress: From petrosa-apps namespace (port 8000, 9090)
- Ingress: From monitoring namespace (port 9090)
- Egress: To NATS (port 4222)
- Egress: To External MySQL (port 3306)
- Egress: To MongoDB (port 27017)
- Egress: To Binance API (port 443)
- Egress: To OTLP (ports 4317, 4318)

---

## 🎛️ Scaling

### Automatic Scaling (HPA)
- Scales based on CPU utilization (target: 70%)
- Scales based on memory utilization (target: 80%)
- Min replicas: 3
- Max replicas: 10
- Scale-up: Fast (100% in 30s, or 2 pods)
- Scale-down: Gradual (50% in 60s, stabilization 5min)

### Manual Scaling

```bash
# Scale to specific replica count
kubectl scale deployment petrosa-data-manager --replicas=5 -n petrosa-apps --insecure-skip-tls-verify

# Disable HPA (if needed)
kubectl delete hpa petrosa-data-manager-hpa -n petrosa-apps --insecure-skip-tls-verify
```

---

## 🛠️ Troubleshooting

### Issue: Pods Not Starting

```bash
# Check pod events
kubectl describe pod -l app=data-manager -n petrosa-apps --insecure-skip-tls-verify

# Common issues:
# - ImagePullBackOff: Check Docker Hub image exists
# - CrashLoopBackOff: Check logs for errors
# - Pending: Check resource availability
```

### Issue: Database Connection Failed

```bash
# Check logs
kubectl logs -l app=data-manager -n petrosa-apps --insecure-skip-tls-verify | grep -i "database\|mysql\|mongodb"

# Verify secrets
kubectl get secret petrosa-sensitive-credentials -n petrosa-apps --insecure-skip-tls-verify
kubectl describe secret petrosa-sensitive-credentials -n petrosa-apps --insecure-skip-tls-verify

# Test connection from pod
kubectl exec -it deploy/petrosa-data-manager -n petrosa-apps --insecure-skip-tls-verify -- /bin/bash
# Inside pod: Test MySQL and MongoDB connectivity
```

### Issue: NATS Connection Failed

```bash
# Check NATS server
kubectl get pods -n nats --insecure-skip-tls-verify

# Check NATS URL in configmap
kubectl get configmap petrosa-common-config -n petrosa-apps --insecure-skip-tls-verify -o yaml | grep NATS_URL
```

### Issue: No Metrics Being Computed

```bash
# Check analytics scheduler
kubectl logs -l app=data-manager -n petrosa-apps --insecure-skip-tls-verify | grep "Analytics cycle"

# Check if sufficient data exists
kubectl logs -l app=data-manager -n petrosa-apps --insecure-skip-tls-verify | grep "Insufficient data"

# Verify socket-client is publishing
kubectl logs -l app=socket-client -n petrosa-apps --insecure-skip-tls-verify | grep "Published to NATS"
```

---

## 📦 CI/CD Pipeline

### Continuous Integration (ci.yml)

**Triggers**: Push to any branch, Pull requests

**Jobs**:
1. **lint** - flake8, black, ruff, mypy
2. **test** - pytest with coverage
3. **security** - bandit security scan
4. **build** - Docker build verification

### Continuous Deployment (deploy.yml)

**Trigger**: Push to `main` branch

**Process**:
1. Create semantic version tag
2. Build Docker image with version tag
3. Push to Docker Hub (yurisa2/petrosa-data-manager)
4. Checkout `petrosa_k8s` repository
5. Update VERSION_PLACEHOLDER in manifests
6. Apply manifests to `petrosa-apps` namespace
7. Wait for rollout completion
8. Verify deployment

**Secrets Required** (Already configured at organization level):
- `DOCKERHUB_USERNAME`
- `DOCKERHUB_TOKEN`
- `KUBE_CONFIG_DATA`
- `GITHUB_TOKEN` (automatic)

---

## 🎯 Quick Deployment Commands

### Deploy Latest Version

```bash
# Option 1: Via GitHub Actions (Automatic)
git push origin main
# Wait for GitHub Actions to complete (~5-10 minutes)

# Option 2: Via petrosa_k8s (Manual)
cd /Users/yurisa2/petrosa/petrosa_k8s
kubectl apply -f k8s/data-manager/ -n petrosa-apps --insecure-skip-tls-verify
```

### Check Deployment Status

```bash
# Pod status
kubectl get pods -l app=data-manager -n petrosa-apps --insecure-skip-tls-verify

# Deployment info
kubectl get deployment petrosa-data-manager -n petrosa-apps --insecure-skip-tls-verify

# Service info
kubectl get svc petrosa-data-manager -n petrosa-apps --insecure-skip-tls-verify

# HPA status
kubectl get hpa petrosa-data-manager-hpa -n petrosa-apps --insecure-skip-tls-verify
```

### View Logs

```bash
# All pods
kubectl logs -l app=data-manager -n petrosa-apps --tail=100 --insecure-skip-tls-verify

# Follow logs
kubectl logs -l app=data-manager -n petrosa-apps --tail=100 -f --insecure-skip-tls-verify

# Specific pod
kubectl logs petrosa-data-manager-xxxxx-xxxxx -n petrosa-apps --insecure-skip-tls-verify
```

### Access API

```bash
# Port forward
kubectl port-forward svc/petrosa-data-manager 8000:80 -n petrosa-apps --insecure-skip-tls-verify

# Test endpoints
curl http://localhost:8000/
curl http://localhost:8000/health/liveness
curl http://localhost:8000/health/readiness
curl http://localhost:8000/docs  # OpenAPI documentation

# Test data endpoint
curl "http://localhost:8000/data/candles?pair=BTCUSDT&period=1h&limit=10"

# Test analytics
curl "http://localhost:8000/analysis/market-overview?pairs=BTCUSDT,ETHUSDT"
```

---

## 🔄 Update/Rollback

### Update to New Version

```bash
# Push changes to trigger deployment
git push origin main

# Or manually update image tag in petrosa_k8s
cd /Users/yurisa2/petrosa/petrosa_k8s
# Edit k8s/data-manager/deployment.yaml to use new version
kubectl apply -f k8s/data-manager/deployment.yaml -n petrosa-apps --insecure-skip-tls-verify
```

### Rollback to Previous Version

```bash
# Rollback deployment
kubectl rollout undo deployment/petrosa-data-manager -n petrosa-apps --insecure-skip-tls-verify

# Or rollback to specific revision
kubectl rollout history deployment/petrosa-data-manager -n petrosa-apps --insecure-skip-tls-verify
kubectl rollout undo deployment/petrosa-data-manager --to-revision=N -n petrosa-apps --insecure-skip-tls-verify
```

---

## 📊 Monitoring

### Prometheus Metrics

```bash
# Port forward metrics endpoint
kubectl port-forward svc/petrosa-data-manager 9090:9090 -n petrosa-apps --insecure-skip-tls-verify

# Query metrics
curl http://localhost:9090/metrics

# Key metrics:
# data_manager_nats_connection_status
# data_manager_messages_received_total
# data_manager_messages_processed_total
# data_manager_message_processing_seconds
```

### Grafana Dashboards

Metrics are automatically scraped by Prometheus (via annotations on pods) and available in Grafana.

---

## 🌍 Service Discovery

### Internal DNS

The Data Manager API is accessible within the cluster:

```
http://petrosa-data-manager.petrosa-apps.svc.cluster.local
http://petrosa-data-manager.petrosa-apps.svc.cluster.local:80         # API
http://petrosa-data-manager.petrosa-apps.svc.cluster.local:9090       # Metrics
```

### From Other Pods in petrosa-apps Namespace

```
http://petrosa-data-manager
http://petrosa-data-manager:80
```

---

## 📝 Summary

### What's Deployed

✅ **Application**: petrosa-data-manager (petrosa-apps namespace)  
✅ **Configuration**: ConfigMap + Shared ConfigMap  
✅ **Secrets**: Uses existing petrosa-sensitive-credentials  
✅ **Database**: External MySQL + MongoDB (already configured)  
✅ **Message Bus**: NATS (already deployed)  
✅ **Scaling**: HPA 3-10 replicas  
✅ **Monitoring**: Prometheus metrics exposed  
✅ **Security**: Network policies, non-root containers  

### What's NOT Deployed

❌ **Support Services** - MySQL and MongoDB are external, not deployed to cluster  
❌ **Redis** - Not needed for MVP, can be added later for caching  
❌ **Ingress** - Internal service only, accessed via ClusterIP  

---

## 🚀 Ready to Deploy!

The Petrosa Data Manager is fully configured and ready for deployment:

1. **Push to main** → GitHub Actions handles everything
2. **Verify health** → Check logs and health endpoints
3. **Monitor metrics** → Prometheus scrapes automatically
4. **Access API** → Via internal service DNS or port-forward

**Deployment Location**: `petrosa_k8s/k8s/data-manager/`  
**Target Namespace**: `petrosa-apps`  
**Dependencies**: All external (MySQL, MongoDB, NATS)  
**Status**: ✅ **PRODUCTION READY**

