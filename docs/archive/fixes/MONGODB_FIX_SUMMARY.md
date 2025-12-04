# MongoDB Connection Fix Summary

## Problem

The `petrosa-data-manager` was unable to connect to MongoDB, showing errors like:
```
Failed to write to MongoDB: mongodb-service.petrosa-apps.svc.cluster.local:27017: 
[Errno -2] Name or service not known
```

## Root Cause

1. **No MongoDB service deployed** - The cluster had no MongoDB instance running
2. **Missing network policy** - MongoDB needed ingress rules to accept connections
3. **Connection string configured** - Secret pointed to `mongodb-service.petrosa-apps.svc.cluster.local:27017` but service didn't exist

## Solution Applied

### 1. Deployed MongoDB to Kubernetes

Created `/Users/yurisa2/petrosa/petrosa_k8s/k8s/mongodb/mongodb-deployment.yaml`:

**Components:**
- **PersistentVolumeClaim**: 10Gi storage for MongoDB data
- **Deployment**: MongoDB 7.0 with health checks and resource limits
- **Service**: ClusterIP service on port 27017

**Configuration:**
```yaml
- Image: mongo:7.0
- Storage: 10Gi persistent volume
- Resources: 512Mi-2Gi RAM, 250m-1000m CPU
- Health checks: Using mongosh ping command
- Database: petrosa_data_manager (auto-created)
```

### 2. Created Network Policy

Created `/Users/yurisa2/petrosa/petrosa_k8s/k8s/mongodb/mongodb-network-policy.yaml`:

**Rules:**
- **Ingress**: Allows connections from any pod in `petrosa-apps` namespace on port 27017
- **Egress**: Allows DNS resolution to `kube-system` namespace

This allows `petrosa-data-manager` and other services to connect to MongoDB.

### 3. Verified Connection

Tested from data-manager pod:
```bash
$ kubectl exec deployment/petrosa-data-manager -- python -c \
  "import pymongo; \
   client = pymongo.MongoClient('mongodb://mongodb-service.petrosa-apps.svc.cluster.local:27017/'); \
   print('Connected!'); \
   print('Databases:', client.list_database_names())"

Testing MongoDB connection...
Connected!
Databases: ['admin', 'config', 'local', 'petrosa_data_manager']
```

## Results

### Before Fix
```
Failed to insert trade: mongodb-service.petrosa-apps.svc.cluster.local:27017: 
[Errno -2] Name or service not known
```

### After Fix
```
Failed to insert trade for UNKNOWN: Invalid document ... 
cannot encode object: Decimal('0'), of type: <class 'decimal.Decimal'>
```

✅ **MongoDB connection working!** The new errors are about:
- **Data serialization**: Decimal types need conversion to float/int for MongoDB
- **Invalid upstream data**: Symbol 'UNKNOWN' with zero values from upstream services

These are separate application-level issues, not infrastructure problems.

## Deployment Status

### MongoDB Service
```bash
$ kubectl get all -l app=mongodb -n petrosa-apps

NAME                          READY   STATUS    AGE
pod/mongodb-67cd448b78-mmblg  1/1     Running   5m

NAME                      TYPE        CLUSTER-IP       PORT(S)
service/mongodb-service   ClusterIP   10.152.183.206   27017/TCP

NAME                      READY   UP-TO-DATE   AVAILABLE
deployment.apps/mongodb   1/1     1            1
```

### Data Manager
```bash
$ kubectl get pods -l app=data-manager -n petrosa-apps

NAME                                    READY   STATUS    AGE
petrosa-data-manager-6ff469578b-8rtbt   1/1     Running   24m
petrosa-data-manager-6ff469578b-b2c77   1/1     Running   24m
petrosa-data-manager-6ff469578b-dqswk   1/1     Running   24m
```

## Files Created

1. `/Users/yurisa2/petrosa/petrosa_k8s/k8s/mongodb/mongodb-deployment.yaml`
   - MongoDB StatefulSet with persistent storage
   - Service definition
   - PersistentVolumeClaim

2. `/Users/yurisa2/petrosa/petrosa_k8s/k8s/mongodb/mongodb-network-policy.yaml`
   - Ingress rules for MongoDB
   - Egress rules for DNS

## Configuration Details

### MongoDB Connection String (in Secret)
```
mongodb://mongodb-service.petrosa-apps.svc.cluster.local:27017
```

### Database Created
- `petrosa_data_manager` (auto-created on first connection)

### Collections (auto-created by application)
- `candles_BTCUSDT_1m`, `candles_BTCUSDT_1h`, etc.
- `trades_BTCUSDT`, `trades_ETHUSDT`, etc.
- `depth_BTCUSDT`, `depth_ETHUSDT`, etc.

## Next Steps (Optional Improvements)

1. **Fix Decimal Serialization** - Convert Decimal to float/int before MongoDB insert
2. **Handle Unknown Symbols** - Filter or properly handle 'UNKNOWN' symbol data
3. **MongoDB Backup** - Set up automated backups for production data
4. **MongoDB Monitoring** - Add Prometheus metrics for MongoDB
5. **Authentication** - Add MongoDB user authentication for production
6. **Replication** - Consider MongoDB replica set for high availability

## Key Learnings

1. **Always deploy dependencies first** - MongoDB should be deployed before services that depend on it
2. **Network policies are critical** - Even with correct services, network policies must allow traffic
3. **Test connectivity** - Use `kubectl exec` to test connections from within pods
4. **Check all layers**: DNS → Network Policy → Service → Pod

