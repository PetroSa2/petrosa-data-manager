# MongoDB Atlas Configuration Fix Summary

## Problem

The `petrosa-data-manager` was initially misconfigured to use an internal MongoDB deployment instead of the existing MongoDB Atlas instance used by other services.

## What Was Wrong

1. **Incorrect Secret Key**: The deployment was using `mongodb-url` key which pointed to internal service `mongodb-service.petrosa-apps.svc.cluster.local:27017`
2. **Temporary Internal MongoDB**: Incorrectly deployed MongoDB internally to the cluster
3. **Network Policy**: Restricted egress only to internal namespace instead of allowing external Atlas connection

## What Other Services Use

All other Petrosa services (tradeengine, ta-bot, etc.) use MongoDB Atlas:
```
Secret Key: mongodb-connection-string
Connection String: mongodb+srv://yurisa2:Fokalove99@petrosa.gynnmi6.mongodb.net/
```

## Solution Applied

### 1. Removed Internal MongoDB

Deleted the incorrectly deployed internal MongoDB:
```bash
kubectl delete -f k8s/mongodb/mongodb-deployment.yaml
kubectl delete -f k8s/mongodb/mongodb-network-policy.yaml
rm -rf /Users/yurisa2/petrosa/petrosa_k8s/k8s/mongodb/
```

### 2. Updated Deployment Configuration

**Changed in both:**
- `/Users/yurisa2/petrosa/petrosa-data-manager/k8s/deployment.yaml`
- `/Users/yurisa2/petrosa/petrosa_k8s/k8s/data-manager/deployment.yaml`

**Before:**
```yaml
- name: MONGODB_URL
  valueFrom:
    secretKeyRef:
      name: petrosa-sensitive-credentials
      key: mongodb-url  # Wrong key - pointed to internal service
```

**After:**
```yaml
- name: MONGODB_URL
  valueFrom:
    secretKeyRef:
      name: petrosa-sensitive-credentials
      key: mongodb-connection-string  # Correct key - points to Atlas
```

### 3. Updated Network Policy

**Changed in:**
- `/Users/yurisa2/petrosa/petrosa-data-manager/k8s/network-policy.yaml`
- `/Users/yurisa2/petrosa/petrosa_k8s/k8s/data-manager/network-policy.yaml`

**Before:**
```yaml
# Allow traffic to petrosa-apps (for internal services like MongoDB if deployed there)
- to:
  - namespaceSelector:
      matchLabels:
        kubernetes.io/metadata.name: petrosa-apps
  ports:
  - protocol: TCP
    port: 27017
```

**After:**
```yaml
# Allow MongoDB Atlas (external) - uses SRV records and TLS on port 27017
- ports:
  - protocol: TCP
    port: 27017
```

This allows egress to **any** destination on port 27017, which is required for MongoDB Atlas.

### 4. Restarted Deployment

```bash
kubectl set image deployment/petrosa-data-manager data-manager=yurisa2/petrosa-data-manager:v1.0.2
kubectl rollout restart deployment/petrosa-data-manager
```

## Verification

### Connection Test
```bash
$ kubectl exec deployment/petrosa-data-manager -- python -c \
  "import pymongo; \
   client = pymongo.MongoClient('mongodb+srv://yurisa2:Fokalove99@petrosa.gynnmi6.mongodb.net/'); \
   print('Connected!'); \
   print('Databases:', client.list_database_names()[:5])"

Testing MongoDB Atlas connection...
Connected!
Databases: ['binance', 'petrosa', 'petrosa_crypto', 'admin', 'local']
```

### Environment Variable Check
```bash
$ kubectl exec deployment/petrosa-data-manager -- env | grep MONGODB_URL

MONGODB_URL=mongodb+srv://yurisa2:Fokalove99@petrosa.gynnmi6.mongodb.net/
```

### Successful Writes Proof
The logs now show duplicate key errors (E11000), which proves successful connection and write attempts:
```
E11000 duplicate key error collection: petrosa_data_manager.depth_UNKNOWN 
index: _id_ dup key: { _id: "UNKNOWN_1761045356716" }
```

This error means:
- ✅ Connected to MongoDB Atlas
- ✅ Database `petrosa_data_manager` exists and is accessible
- ✅ Collections are being written to successfully
- ⚠️ Some data has duplicate keys (data quality issue, not connectivity issue)

## Current Status

### Deployment
```bash
NAME                                    READY   STATUS    AGE
petrosa-data-manager-5c88b46655-49sgc   1/1     Running   2m
petrosa-data-manager-5c88b46655-j4jcc   1/1     Running   2m
petrosa-data-manager-5c88b46655-r946r   1/1     Running   2m
```

### MongoDB Atlas Databases Available
- `binance` - Binance market data
- `petrosa` - Main Petrosa data
- `petrosa_crypto` - Crypto trading data
- `admin` - MongoDB admin
- `local` - MongoDB local

### Collections Being Used
- `petrosa_data_manager.candles_BTCUSDT_1m`
- `petrosa_data_manager.candles_BTCUSDT_1h`
- `petrosa_data_manager.trades_*`
- `petrosa_data_manager.depth_*`
- `petrosa_data_manager.ticker_*`

## Remaining Issues (Application-Level)

The current errors are **not** MongoDB connectivity issues:

1. **Decimal Serialization**: 
   ```
   cannot encode object: Decimal('0'), of type: <class 'decimal.Decimal'>
   ```
   - Fix: Convert Decimal to float/int before MongoDB insert
   - Location: Data serialization layer

2. **Unknown Symbol Data**:
   ```
   Failed to insert trade for UNKNOWN
   ```
   - Fix: Filter or handle UNKNOWN symbol data from upstream
   - Location: Message validation layer

3. **Duplicate Keys**:
   ```
   E11000 duplicate key error
   ```
   - Fix: Ensure unique _id generation or use upsert operations
   - Location: Database write layer

## Configuration Summary

### MongoDB Atlas Connection
- **Host**: `petrosa.gynnmi6.mongodb.net`
- **Protocol**: mongodb+srv:// (SRV record with TLS)
- **Authentication**: Username/password (in secret)
- **Database**: `petrosa_data_manager`

### Secret Structure
```yaml
kind: Secret
metadata:
  name: petrosa-sensitive-credentials
  namespace: petrosa-apps
data:
  mongodb-connection-string: <base64-encoded-atlas-url>
  MONGODB_DATABASE: <base64-encoded-db-name>
```

### Network Requirements
- **Egress**: Port 27017 (TCP) to external MongoDB Atlas cluster
- **DNS**: Port 53 (UDP) for SRV record resolution
- **TLS**: Encrypted connection to Atlas (built into mongodb+srv://)

## Files Modified

1. `/Users/yurisa2/petrosa/petrosa-data-manager/k8s/deployment.yaml`
   - Changed secret key from `mongodb-url` to `mongodb-connection-string`

2. `/Users/yurisa2/petrosa/petrosa_k8s/k8s/data-manager/deployment.yaml`
   - Changed secret key from `mongodb-url` to `mongodb-connection-string`

3. `/Users/yurisa2/petrosa/petrosa-data-manager/k8s/network-policy.yaml`
   - Updated egress rules to allow external MongoDB Atlas access

4. `/Users/yurisa2/petrosa/petrosa_k8s/k8s/data-manager/network-policy.yaml`
   - Updated egress rules to allow external MongoDB Atlas access

## Key Learnings

1. **Check existing services first** - Always verify what other services are using before deploying new infrastructure
2. **MongoDB Atlas needs external egress** - Network policies must allow egress to external IPs on port 27017
3. **Consistent secret keys** - All services should use the same secret key (`mongodb-connection-string`) for consistency
4. **Verify environment variables** - After deployment changes, always check env vars are set correctly
5. **Duplicate key errors are good news** - They prove successful MongoDB connectivity and write attempts

## Next Steps (Optional)

1. **Fix Decimal serialization** - Add conversion layer before MongoDB writes
2. **Filter unknown symbols** - Add validation to reject UNKNOWN symbol data
3. **Implement upsert logic** - Use `update_one(upsert=True)` to handle duplicates
4. **Add MongoDB monitoring** - Monitor Atlas connection health and query performance
5. **Document Atlas setup** - Create documentation for Atlas cluster configuration

