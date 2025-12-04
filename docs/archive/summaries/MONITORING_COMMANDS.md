# Monitoring Commands for Petrosa Data Manager

## Quick Health Check

```bash
# Check pod status and count
kubectl --kubeconfig=k8s/kubeconfig.yaml -n petrosa-apps get pods -l app=data-manager

# Should show exactly 3 pods in Running state
```

## Error Log Monitoring

```bash
# Count error messages (should be very low or zero)
kubectl --kubeconfig=k8s/kubeconfig.yaml -n petrosa-apps logs -l app=data-manager --tail=1000 | grep -i error | wc -l

# Check for audit/analytics errors (should be none)
kubectl --kubeconfig=k8s/kubeconfig.yaml -n petrosa-apps logs -l app=data-manager --tail=1000 | grep -E "audit|analytics" | grep -i error

# Check for storage errors (should be at debug level, not visible)
kubectl --kubeconfig=k8s/kubeconfig.yaml -n petrosa-apps logs -l app=data-manager --tail=1000 | grep "Failed to store"
```

## Verify Configuration

```bash
# Verify schedulers are disabled
kubectl --kubeconfig=k8s/kubeconfig.yaml -n petrosa-apps logs -l app=data-manager --tail=100 | grep -E "Auditor is disabled|Analytics is disabled"

# Should show:
# - "Auditor is disabled"
# - "Analytics is disabled"
```

## NATS Consumer Health

```bash
# Check NATS connection status
kubectl --kubeconfig=k8s/kubeconfig.yaml -n petrosa-apps logs -l app=data-manager --tail=200 | grep NATS

# Should show:
# - "Successfully connected to NATS"
# - "Successfully subscribed to NATS subject"
```

## Database Connection Status

```bash
# Check database initialization
kubectl --kubeconfig=k8s/kubeconfig.yaml -n petrosa-apps logs -l app=data-manager --tail=100 | grep -i database

# Should show:
# - "Database connections initialized successfully"
```

## Resource Usage

```bash
# Check pod resource consumption
kubectl --kubeconfig=k8s/kubeconfig.yaml -n petrosa-apps top pods -l app=data-manager

# With 3 pods and no schedulers, CPU/memory should be stable and lower
```

## Continuous Monitoring

```bash
# Follow logs in real-time (Ctrl+C to stop)
kubectl --kubeconfig=k8s/kubeconfig.yaml -n petrosa-apps logs -l app=data-manager -f --tail=50

# Watch for any unexpected errors or issues
```

## Grafana Verification

In your Grafana dashboard, you should see:

1. **Log Volume**: Significantly reduced (90%+ decrease expected)
2. **Error Rate**: Near zero for data-manager service
3. **Log Levels**: Mostly INFO and DEBUG, very few WARNINGS, almost no ERRORS
4. **Common Messages**: 
   - "Skipping message with missing or invalid symbol" (WARNING - expected)
   - Normal NATS message processing (DEBUG)
   - No audit/analytics cycle errors

## Alert Conditions

Set up alerts for:
- **High Error Rate**: More than 10 errors per minute
- **Pod Restarts**: Any pod restarting frequently
- **NATS Disconnections**: Connection failures to NATS
- **Resource Exhaustion**: Memory > 1.5GB or CPU > 800m

## Rollback if Needed

If any critical issues arise:

```bash
# Scale back up if needed (only if 3 pods insufficient)
kubectl --kubeconfig=k8s/kubeconfig.yaml -n petrosa-apps scale deployment petrosa-data-manager --replicas=5

# Re-enable schedulers if required (edit configmap)
kubectl --kubeconfig=k8s/kubeconfig.yaml -n petrosa-apps edit configmap petrosa-data-manager-config
# Set ENABLE_AUDITOR, ENABLE_BACKFILLER, ENABLE_ANALYTICS to "true"

# Restart deployment to apply changes
kubectl --kubeconfig=k8s/kubeconfig.yaml -n petrosa-apps rollout restart deployment petrosa-data-manager
```

## Expected Normal Behavior

✅ **Good Signs:**
- 3 pods running steadily
- Minimal log output (mostly processing messages)
- No audit/analytics cycle logs
- NATS connection stable
- Warnings about invalid symbols (expected from upstream)

❌ **Warning Signs:**
- Pods restarting frequently
- Errors about NATS connection
- Database connection failures
- Memory continuously increasing
- More than 3 pods running

## Next Steps

1. **Monitor for 24 hours** to ensure stability
2. **Check Grafana** for log volume reduction
3. **Review any new error patterns** that emerge
4. **Consider implementing leader election** for future scheduler re-enabling

