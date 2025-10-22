# Data Manager Auditor

## Overview

The Auditor is a critical component of the Petrosa Data Manager that ensures data quality, completeness, and consistency across all trading datasets. It runs periodic checks to detect gaps, duplicates, and data quality issues, providing automated healing through the auto-backfill system.

## Architecture

### Components

1. **Audit Scheduler** (`data_manager/auditor/scheduler.py`)
   - Orchestrates periodic audit cycles
   - Implements leader election for multi-replica deployments
   - Coordinates gap detection, duplicate detection, and health scoring
   - Collects and exposes Prometheus metrics

2. **Gap Detector** (`data_manager/auditor/gap_detector.py`)
   - Detects missing data ranges in time series
   - Configurable gap tolerance and minimum gap size
   - Optional auto-trigger of backfill jobs for detected gaps
   - Logs gaps to audit repository with severity levels

3. **Duplicate Detector** (`data_manager/auditor/duplicate_detector.py`)
   - Identifies duplicate records based on timestamps
   - Configurable duplicate removal strategies
   - Logs duplicate removal actions to audit repository

4. **Health Scorer** (`data_manager/auditor/health_scorer.py`)
   - Calculates comprehensive health metrics for datasets
   - Scores based on completeness, freshness, and consistency
   - Stores health metrics for trending and alerting

5. **Leader Election** (`data_manager/leader_election.py`)
   - MongoDB-based distributed leader election
   - Prevents duplicate work across multiple replicas
   - Automatic failover on pod crashes
   - Heartbeat mechanism for leader health monitoring

## Leader Election Mechanism

### How It Works

The Auditor uses MongoDB-based leader election to ensure only one pod runs the audit scheduler at a time across multiple replicas.

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Pod 1     │     │   Pod 2     │     │   Pod 3     │
│  LEADER ✓   │     │  FOLLOWER   │     │  FOLLOWER   │
└──────┬──────┘     └─────────────┘     └─────────────┘
       │
       │ Heartbeat (every 10s)
       ▼
┌─────────────────────────────────────────────────────┐
│         MongoDB leader_election Collection          │
│  { status: "leader", pod_id: "pod1", last_heartbeat }│
└─────────────────────────────────────────────────────┘
```

### Election Process

1. **Startup**: Each pod attempts to become leader by writing to MongoDB
2. **Atomic Write**: MongoDB's upsert ensures only one pod wins
3. **Heartbeat**: Leader sends heartbeat every 10 seconds
4. **Stale Detection**: If no heartbeat for 30 seconds, leader is considered stale
5. **Automatic Failover**: Followers detect stale leader and elect new one

### Configuration

```yaml
# Enable/disable leader election
ENABLE_LEADER_ELECTION: "true"

# Heartbeat frequency (seconds)
LEADER_ELECTION_HEARTBEAT_INTERVAL: "10"

# Stale leader timeout (seconds)
LEADER_ELECTION_TIMEOUT: "30"
```

### MongoDB Collections

**leader_election**:
- `status`: "leader" or "follower"
- `pod_id`: Unique pod identifier
- `elected_at`: Timestamp of election
- `last_heartbeat`: Timestamp of last heartbeat
- `updated_at`: Last update timestamp

**Indexes**:
- `status` - Fast leader lookup
- `pod_id` - Pod-specific queries
- `last_heartbeat` (TTL) - Automatic cleanup

## Gap Detection Algorithm

### Detection Logic

```python
1. Fetch all candles in time range
2. Sort by timestamp
3. Calculate expected interval based on timeframe
4. For each consecutive pair:
   - Calculate time difference
   - If difference > expected + tolerance:
     - Create GapInfo with start, end, duration
     - Log gap to audit repository
     - Optionally trigger backfill
```

### Gap Tolerance

```yaml
# Allow up to 60 seconds variance
GAP_TOLERANCE_SECONDS: "60"

# Only report gaps larger than 2 minutes
MIN_GAP_SIZE_SECONDS: "120"
```

### Auto-Backfill Integration

When `ENABLE_AUTO_BACKFILL` is true:

```python
if gap.duration_seconds >= MIN_AUTO_BACKFILL_GAP:
    # Create BackfillRequest
    # Submit to BackfillOrchestrator
    # Log backfill trigger
    # Update metrics
```

Configuration:

```yaml
# Enable automatic backfill for detected gaps
ENABLE_AUTO_BACKFILL: "false"  # Conservative default

# Minimum gap size to trigger backfill (1 hour)
MIN_AUTO_BACKFILL_GAP: "3600"

# Maximum concurrent backfill jobs
MAX_AUTO_BACKFILL_JOBS: "5"
```

## Health Scoring Methodology

### Metrics Calculated

1. **Completeness** (0-100%):
   ```
   completeness = (actual_records / expected_records) * 100
   ```

2. **Freshness Score** (0-100):
   ```
   freshness_score = max(0, 100 - (age_seconds / 300) * 100)
   # Penalizes data older than 5 minutes
   ```

3. **Consistency Score** (0-100):
   ```
   consistency = 100.0
   consistency -= min(gaps_count * 10, 50)  # Gap penalty
   consistency -= min(duplicates_count * 5, 30)  # Duplicate penalty
   consistency = max(consistency, 0)
   ```

4. **Quality Score** (0-100):
   ```
   quality = completeness * 0.4 +
             consistency * 0.4 +
             freshness * 0.2
   ```

### Health Levels

| Quality Score | Status | Description |
|---------------|--------|-------------|
| 90-100 | Healthy | Excellent data quality |
| 70-89 | Degraded | Some quality issues detected |
| 50-69 | Unhealthy | Significant quality problems |
| 0-49 | Critical | Severe data quality issues |

## Duplicate Detection and Removal

### Detection

Duplicates are identified by:
- Same symbol, timeframe, and timestamp
- Multiple records with identical natural keys

### Removal Strategies

#### 1. Keep Newest (Default)
```yaml
DUPLICATE_RESOLUTION_STRATEGY: "keep_newest"
```
- Keeps the record with the most recent MongoDB `_id`
- Removes older duplicates
- Best for data that may have corrections

#### 2. Keep Oldest
```yaml
DUPLICATE_RESOLUTION_STRATEGY: "keep_oldest"
```
- Keeps the record with the oldest MongoDB `_id`
- Removes newer duplicates
- Best for preserving original data

#### 3. Manual
```yaml
DUPLICATE_RESOLUTION_STRATEGY: "manual"
```
- Only detects duplicates, doesn't remove
- Requires manual intervention
- Best for investigation/validation

### Configuration

```yaml
# Enable automatic duplicate removal
ENABLE_DUPLICATE_REMOVAL: "false"  # Conservative default

# Removal strategy
DUPLICATE_RESOLUTION_STRATEGY: "keep_newest"
```

## Monitoring and Metrics

### Prometheus Metrics

```
# Audit cycle duration
data_manager_audit_cycle_seconds{} histogram

# Gaps detected
data_manager_audit_gaps_detected_total{symbol, timeframe} counter

# Duplicates detected
data_manager_audit_duplicates_detected_total{symbol, timeframe} counter

# Health scores
data_manager_audit_health_score{symbol, timeframe} gauge

# Leader status
data_manager_audit_leader_status{} gauge  # 1=leader, 0=follower

# Auto-backfills triggered
data_manager_audit_backfills_triggered_total{symbol, timeframe} counter

# Duplicates removed
data_manager_duplicates_removed_total{symbol, timeframe} counter
```

### Grafana Dashboards

Recommended panels:

1. **Leader Election Status**
   - Current leader pod
   - Leadership changes over time
   - Heartbeat health

2. **Audit Cycle Performance**
   - Cycle duration (P50, P95, P99)
   - Symbols audited per cycle
   - Audit frequency

3. **Data Quality Trends**
   - Quality scores over time per symbol/timeframe
   - Gap detection rate
   - Duplicate detection rate

4. **Auto-Backfill Activity**
   - Backfills triggered
   - Backfill success rate
   - Average gap size

### Health Check Endpoints

```bash
# Leader election status
curl http://data-manager:8000/health/leader

# Audit scheduler status
curl http://data-manager:8000/health/audit-status

# Overall system health
curl http://data-manager:8000/health/readiness
```

## Deployment and Rollout

### Phase 1: Deploy with Auditor Disabled

```yaml
# k8s/configmap.yaml
ENABLE_AUDITOR: "false"
```

Deploy and verify:
- Leader election code is present but not running
- No impact on existing functionality
- Metrics endpoints available

### Phase 2: Enable on Single Replica

```bash
# Scale down to 1 replica
kubectl scale deployment data-manager --replicas=1

# Enable auditor
kubectl patch configmap petrosa-data-manager-config \
  -p '{"data":{"ENABLE_AUDITOR":"true"}}'

# Restart deployment
kubectl rollout restart deployment data-manager
```

Monitor for 24 hours:
- Check audit cycle logs
- Verify gap detection
- Monitor performance impact
- Review metrics in Grafana

### Phase 3: Enable on All Replicas

```bash
# Scale up to 3 replicas
kubectl scale deployment data-manager --replicas=3
```

Verify:
- Only one pod runs auditor (check logs for "LEADER" designation)
- Followers log "FOLLOWER" status
- Automatic failover on leader pod restart
- No duplicate audit work

### Phase 4: Enable Auto-Backfill (Optional)

After validating gap detection:

```yaml
ENABLE_AUTO_BACKFILL: "true"
MIN_AUTO_BACKFILL_GAP: "3600"  # Start conservative (1 hour)
```

Monitor:
- Backfill job creation rate
- Database load from backfills
- Gap resolution effectiveness

### Phase 5: Enable Duplicate Removal (Optional)

After validating duplicate detection:

```yaml
ENABLE_DUPLICATE_REMOVAL: "true"
DUPLICATE_RESOLUTION_STRATEGY: "keep_newest"
```

Monitor:
- Duplicate removal rate
- Data integrity after removal
- No unintended data loss

## Troubleshooting

### Issue: Multiple Pods Running Auditor

**Symptoms**:
- Duplicate audit cycles in logs
- Multiple pods log "LEADER" status
- 3x metrics increments

**Diagnosis**:
```bash
# Check leader election status on each pod
for pod in $(kubectl get pods -l app=data-manager -o name); do
  echo "=== $pod ===="
  kubectl exec $pod -- curl -s localhost:8000/health/leader | jq .
done
```

**Resolution**:
1. Check MongoDB connectivity
2. Verify `ENABLE_LEADER_ELECTION: "true"`
3. Check MongoDB leader_election collection
4. Restart pods to re-elect leader

### Issue: No Audit Cycles Running

**Symptoms**:
- No audit cycle logs
- Metrics not updating
- All pods show "FOLLOWER" status

**Diagnosis**:
```bash
# Check if auditor is enabled
kubectl get configmap petrosa-data-manager-config -o yaml | grep ENABLE_AUDITOR

# Check leader election
kubectl exec data-manager-xxx -- curl -s localhost:8000/health/leader
```

**Resolution**:
1. Verify `ENABLE_AUDITOR: "true"`
2. Check database connectivity
3. Manually trigger leader re-election (restart pods)

### Issue: Leader Not Heartbeating

**Symptoms**:
- Leader pod logs errors
- Frequent leader changes
- Metrics show leadership flapping

**Diagnosis**:
```bash
# Check MongoDB connection
kubectl exec data-manager-xxx -- python -c "
import motor.motor_asyncio
import asyncio
client = motor.motor_asyncio.AsyncIOMotorClient('mongodb://...')
asyncio.run(client.admin.command('ping'))
"
```

**Resolution**:
1. Check MongoDB health
2. Review MongoDB network policies
3. Increase `LEADER_ELECTION_TIMEOUT` if network is slow

### Issue: Auto-Backfill Not Triggering

**Symptoms**:
- Gaps detected but no backfill jobs created
- `audit_backfills_triggered_total` metric not incrementing

**Diagnosis**:
```bash
# Check configuration
kubectl get configmap petrosa-data-manager-config -o yaml | grep -A5 "Auto-Backfill"

# Check backfill orchestrator initialization
kubectl logs data-manager-xxx | grep "backfill_orchestrator"
```

**Resolution**:
1. Verify `ENABLE_AUTO_BACKFILL: "true"`
2. Check gap size vs `MIN_AUTO_BACKFILL_GAP`
3. Verify backfill orchestrator is initialized

## Best Practices

### 1. Leader Election

- **Always enable** in multi-replica deployments
- Monitor leader changes (should be rare)
- Alert on frequent leadership changes
- Keep heartbeat interval < timeout/3

### 2. Gap Detection

- Set `GAP_TOLERANCE_SECONDS` based on network latency
- Use `MIN_GAP_SIZE_SECONDS` to avoid noise
- Start with auto-backfill disabled
- Monitor backfill job success rate before enabling auto-backfill

### 3. Duplicate Removal

- Test thoroughly in development first
- Start with `ENABLE_DUPLICATE_REMOVAL: "false"`
- Review duplicate patterns before enabling removal
- Use `keep_newest` for data with corrections
- Use `keep_oldest` for immutable historical data

### 4. Health Scoring

- Tune scoring weights based on business requirements
- Set up alerts for quality scores < 70
- Track health trends over time
- Use health scores for SLA monitoring

### 5. Monitoring

- Create Grafana dashboards for all metrics
- Set up alerts for:
  - Leader election failures
  - Quality scores below threshold
  - High gap/duplicate rates
  - Audit cycle failures
- Review metrics weekly to identify patterns

## Performance Considerations

### Audit Cycle Duration

Expected duration per cycle:
- 5 symbols × 6 timeframes = 30 iterations
- ~1-2 seconds per iteration
- **Total: 30-60 seconds per cycle**

Runs every 5 minutes (300 seconds), so ~20% CPU utilization.

### Database Load

- Gap detection: 1 query per symbol/timeframe
- Duplicate detection: 1 query per symbol/timeframe
- Health calculation: 2 queries per symbol/timeframe
- **Total: ~120 queries per cycle**

At 5-minute intervals: ~0.4 queries/second (negligible load).

### Memory Usage

- Leader election: <1 MB
- Audit scheduler: <10 MB
- Gap detector: Depends on data range (typically <50 MB)
- **Total overhead: <100 MB per pod**

## Security Considerations

1. **MongoDB Access**: Leader election requires write access to MongoDB
2. **Audit Logs**: Contains gap and duplicate information (not sensitive)
3. **Metrics**: Exposed on internal port only (not public)
4. **Health Endpoints**: Internal use only, contain system status

## Future Enhancements

- [ ] Add anomaly detection for price/volume outliers
- [ ] Implement ML-based quality prediction
- [ ] Add cross-symbol correlation checks
- [ ] Support for multiple audit profiles (fast/thorough)
- [ ] Integration with alerting systems (PagerDuty, Slack)
- [ ] Automated quality reporting
- [ ] SLA compliance tracking
- [ ] Historical quality trending and forecasting

## References

- [Leader Election Pattern](../../../petrosa-tradeengine/shared/distributed_lock.py)
- [Gap Detection Algorithm](gap_detector.py)
- [Health Scoring Methodology](health_scorer.py)
- [Kubernetes Deployment](../k8s/deployment.yaml)
- [Configuration Reference](../k8s/configmap.yaml)

