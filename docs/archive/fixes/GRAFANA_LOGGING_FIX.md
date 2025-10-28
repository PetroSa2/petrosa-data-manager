# Data Manager Grafana Logging Fix

## Problem

Logs from the `petrosa-data-manager` service were not appearing in Grafana, despite OpenTelemetry being configured and enabled.

## Root Cause Analysis

### Issue 1: Optional ConfigMap Reference
The deployment had `optional: true` for the `OTEL_EXPORTER_OTLP_ENDPOINT`:

```yaml
- name: OTEL_EXPORTER_OTLP_ENDPOINT
  valueFrom:
    configMapKeyRef:
      name: petrosa-common-config
      key: OTEL_EXPORTER_OTLP_ENDPOINT
      optional: true  # ❌ This allowed pods to start with empty endpoint
```

If the ConfigMap key failed to load (or was empty), the pod would start successfully but with an empty endpoint string.

### Issue 2: Empty String Default in constants.py
```python
OTEL_EXPORTER_OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
```

This defaulted to an empty string when the env var was not set.

### Issue 3: Silent Failure in main.py
```python
if constants.OTEL_ENABLED and constants.OTEL_EXPORTER_OTLP_ENDPOINT:
    attach_logging_handler()
```

Since empty string evaluates to `False`, the logging handler was never attached, and there was no error message indicating why.

## The Fix

### 1. Made OTLP Endpoint Required
Changed `optional: true` to `optional: false` in `k8s/deployment.yaml`:
- Now if the ConfigMap key is missing, the pod won't start
- Makes configuration errors visible immediately

### 2. Added Fallback Default Endpoint
Updated `constants.py` to use the standard Grafana Alloy endpoint as default:
```python
OTEL_EXPORTER_OTLP_ENDPOINT = os.getenv(
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "http://grafana-alloy.observability.svc.cluster.local:4317"
)
```

### 3. Added Comprehensive Debug Logging
Enhanced `main.py` with detailed logging:
- Logs when OpenTelemetry initialization starts
- Shows the endpoint being used
- Explicitly logs when logging handler is attached
- Shows clear error if endpoint is empty
- Logs success message: "✅ OpenTelemetry logging handler attached - logs will be exported to Grafana"

## Verification

After applying these changes:

1. **Check pod startup logs**:
   ```bash
   kubectl logs -n petrosa-apps -l app=data-manager --tail=50 | grep -i otel
   ```
   
   Expected output:
   ```
   Initializing OpenTelemetry with endpoint: http://grafana-alloy.observability.svc.cluster.local:4317
   OpenTelemetry initialized successfully
   Attaching OpenTelemetry logging handler...
   ✅ OpenTelemetry logging handler attached - logs will be exported to Grafana
   ```

2. **Verify logs in Grafana**:
   - Navigate to Grafana Explore
   - Query: `{service_name="petrosa-data-manager"}`
   - Should see application logs appearing in real-time

3. **Check if logs include trace context**:
   - Logs should have trace_id and span_id fields
   - Correlation between traces and logs should work

## Impact

- ✅ **Before**: Logs silently lost, no visibility in Grafana
- ✅ **After**: All logs exported to Grafana with trace correlation
- ✅ **Observability**: Clear error messages if configuration is wrong
- ✅ **Debugging**: Startup logs show exact endpoint being used

## Related Configuration

The OTLP endpoint is defined in `petrosa-common-config` ConfigMap:
```yaml
OTEL_EXPORTER_OTLP_ENDPOINT: http://grafana-alloy.observability.svc.cluster.local:4317
```

This endpoint points to the Grafana Alloy service running in the `observability` namespace.

## Future Prevention

To prevent similar issues in other services:

1. **Audit all services** for `optional: true` on OTEL environment variables
2. **Add fallback defaults** in constants.py for all critical OTEL variables
3. **Add startup logging** to verify OTEL configuration is working
4. **Create health check** that verifies logging export is active

## Testing

After deployment:
```bash
# Test that logs appear in stdout (baseline)
kubectl logs -n petrosa-apps -l app=data-manager --tail=20

# Test that logs appear in Grafana (after ~30 seconds)
# Check Grafana Explore with query: {service_name="petrosa-data-manager"}

# Test trace context is injected
# Logs should have 'trace_id' and 'span_id' fields in JSON format
```

## References

- OpenTelemetry Logging: https://opentelemetry.io/docs/specs/otel/logs/
- Grafana Alloy: https://grafana.com/docs/alloy/
- petrosa-otel package: `/Users/yurisa2/petrosa/petrosa_k8s/petrosa-otel/`

