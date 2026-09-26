# Unified Candle Gap Detection-to-Filling Pipeline

## Overview

This document describes the unified pipeline that detects gaps in MongoDB `candles_*` collections and automatically triggers backfill to restore data completeness. The pipeline bridges three components:

1. **Gap Detection** (continuous + periodic)
2. **Backfill Orchestration** (automatic + health-driven)
3. **Readiness Verification** (depth + freshness gates)

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     Candle Gap Pipeline                         │
│                                                                 │
│  ┌──────────────┐     ┌──────────────┐     ┌───────────────┐  │
│  │ Streaming    │     │ Periodic     │     │ Candle        │  │
│  │ Gap Detector │     │ Gap Detector │     │ Readiness     │  │
│  │ (NATS)       │     │ (5-min)      │     │ Gate          │  │
│  └──────┬───────┘     └──────┬───────┘     └───────┬───────┘  │
│         │                    │                       │          │
│         ▼                    ▼                       │          │
│  ┌──────────────────────────────────────┐           │          │
│  │        Backfill Trigger              │           │          │
│  │  (analytics bridge)                  │           │          │
│  └──────────────┬───────────────────────┘           │          │
│                 │                                    │          │
│                 ▼                                    │          │
│  ┌───────────────────────────────────────┐          │          │
│  │     Backfill Orchestrator             │          │          │
│  │     (fetches from Binance API)        │          │          │
│  └──────────────┬────────────────────────┘          │          │
│                 │                                    │          │
│                 ▼                                    │          │
│  ┌───────────────────────────────────────┐          │          │
│  │     MongoDB candles_* collections     │────────┘          │
│  └───────────────────────────────────────┘                   │
└─────────────────────────────────────────────────────────────────┘
```

## Components

### 1. Streaming Gap Detector (Continuous)

**File:** `data_manager/auditor/streaming_gap_detector.py`

Subscribes to NATS kline events and detects gaps in real-time by tracking the
last-seen timestamp per (symbol, timeframe) pair.

- **Trigger:** Incoming kline event from NATS
- **Detection:** Gap when time between consecutive candles exceeds
  `STREAMING_GAP_TOLERANCE_INTERVALS * timeframe` (2 intervals)
- **Threshold:** `MIN_GAP_DURATION_SECONDS` (180s) — skips micro-gaps from jitter
- **Cap:** `MAX_BACKFILL_WINDOW_SECONDS` (86400s / 24 hours) — prevents massive
  backfill requests
- **Dedup:** 30-second cooldown per (symbol, timeframe) to avoid duplicate triggers

**Activation:** Controlled by `ENABLE_STREAMING_GAP_DETECTION` (default: `true`).
Wired into `AuditScheduler` at startup.

### 2. Periodic Gap Detector (Batch)

**File:** `data_manager/auditor/gap_detector.py`

Runs as part of the audit cycle (every `AUDIT_INTERVAL` seconds, default 300s).
Scans the last 24 hours of candle data for gaps.

- **Detection:** Compares actual candle timestamps against expected intervals
- **Auto-backfill:** When `ENABLE_AUTO_BACKFILL=true` and gap exceeds
  `MIN_AUTO_BACKFILL_GAP`, triggers backfill via the orchestrator
- **Audit logging:** Logs gaps to the audit repository with severity (high/medium)

### 3. Backfill Trigger (Analytics Bridge)

**File:** `data_manager/services/backfill_trigger.py` (NEW — AC4)

Consumes evaluator verdicts (`healthy`/`unhealthy`) and triggers backfill when
data health is poor. Two modes:

- **Audit mode:** Parses the evaluator's worst-gap reason string to extract the
  affected (symbol, timeframe, duration) and triggers targeted backfill.
- **Readiness mode:** Calls `candle_readiness.evaluate_readiness()` and triggers
  backfill for all not-ready collections.

**Dedup:** 5-minute cooldown per verdict key to prevent duplicate triggers.

### 4. Candle Readiness Gate

**File:** `data_manager/maintenance/candle_readiness.py`

Fail-closed gate that answers: *is it safe to promote MongoDB to the primary
execution candle store?*

A collection is **ready** when both hold:

1. **Depth:** At least `CANDLE_WARMUP_MIN_CANDLES` (400) candles
2. **Freshness:** Newest candle is within `CANDLE_WARMUP_FRESHNESS_INTERVALS`
   (3) x timeframe of now

### 5. Backfill Orchestrator

**File:** `data_manager/backfiller/orchestrator.py`

Receives `BackfillRequest` objects and fetches missing candles from the
Binance API. The binance-data-extractor's `extract_klines_gap_filler.py`
(extended in PR #301) writes operational candle data to MongoDB and keeps the optional MySQL historic copy.


### 6. Backfill Request Queue (petrosa-data-manager#320)

**File:** `data_manager/services/backfill_queue.py`

In-memory FIFO queue that bridges the gap when the backfill orchestrator is
unavailable (crash, restart, or `ENABLE_BACKFILLER=false`).  When `ENABLE_AUTO_BACKFILL=true`
but the orchestrator is not running, gaps are **not silently dropped** — they are
collected here and flushed to the orchestrator once it becomes available again.

- **Max size:** `BACKFILL_QUEUE_MAX_SIZE` (default: 100). Oldest requests are dropped
  when full (logged as a warning).
- **Max retries:** `BACKFILL_QUEUE_MAX_RETRIES` (default: 3). Requests that fail flush
  more than this are dropped.
- **Flush interval:** Every audit cycle (default every 60s via `BACKFILL_QUEUE_FLUSH_INTERVAL`).
- **Dedup:** The orchestrator handles dedup internally; the queue is a best-effort
  bridge.

**Activation:** Always active (created at startup regardless of `ENABLE_BACKFILLER`).
The queue is only *used* when the orchestrator is unavailable or its call fails.

**Wiring:** The queue is created in `main.py` and passed to `GapDetector`,
`StreamingGapDetector`, and `BackfillTrigger` at construction time.

## Metrics

| Metric | Description | Labels |
|--------|-------------|--------|
| `data_manager_gap_detection_latency_seconds` | Time from gap start to detection | symbol, timeframe |
| `data_manager_gaps_detected_total` | Total gaps detected by streaming detector | symbol, timeframe, source |
| `data_manager_backfills_triggered_total` | Total backfills triggered by streaming detector | symbol, timeframe, source |
| `data_manager_streaming_detector_status` | Streaming detector status (1=active, 0=inactive) | symbol, timeframe |
| `data_manager_last_seen_candle_timestamp` | Last seen candle timestamp | symbol, timeframe |
| `data_manager_trigger_backfills_total` | Backfill jobs triggered by analytics bridge | trigger_source, symbol, timeframe |
| `data_manager_backfill_request_latency_seconds` | Time from verdict to backfill request | trigger_source |
| `data_manager_candle_read_fallbacks_total` | Candle reads served by non-primary backend | primary, operation |
| `data_manager_gaps_filled_auto_total` | Total gaps auto-filled (auto vs manual fill ratio) | symbol, timeframe |
| `data_manager_gaps_queued_total` | Total gaps queued for later backfill (orchestrator unavailable) | symbol, timeframe |
| `data_manager_backfill_queue_size` | Number of backfill requests waiting in the in-memory queue | (gauge) |
| `data_manager_backfill_queue_flushed_total` | Total backfill requests flushed from queue to orchestrator | symbol, timeframe |
| `data_manager_backfill_queue_failed_total` | Total backfill requests that failed to flush | (counter) |

## Alerting

### Fallback Engagement Alert (AC5)

When the candle read fallback is engaged (primary backend returns no data),
the `data_manager_candle_read_fallbacks_total` counter increments. The following
Prometheus alert fires when fallbacks exceed a threshold:

```yaml
groups:
  - name: candle-pipeline
    rules:
      - alert: CandleReadFallbackRateHigh
        expr: rate(data_manager_candle_read_fallbacks_total[5m]) > 0.1
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Candle read fallback engaged"
          description: "data_manager_candle_read_fallbacks_total rate > 0.1/s for 5m. Primary backend unable to serve candle reads. Check MongoDB health and warm-up status."
          runbook: "https://github.com/PetroSa2/petrosa-data-manager/blob/main/docs/candle-cutover-runbook.md"
```

### Gap Detection Alert

```yaml
      - alert: StreamingGapDetected
        expr: rate(data_manager_gaps_detected_total{source="streaming"}[5m]) > 0
        for: 2m
        labels:
          severity: warning
        annotations:
          summary: "Streaming gap detected"
          description: "Gap detected by streaming detector for {{ $labels.symbol }} {{ $labels.timeframe }}"
```

## Operational Procedures

### Monitoring the Pipeline

1. **Grafana dashboards:**
   - `data_manager_gap_detection_latency_seconds` histogram
   - `data_manager_backfills_triggered_total` counter
   - `data_manager_candle_read_fallbacks_total` counter

2. **Key indicators:**
   - `data_manager_streaming_detector_status == 1` for all (symbol, timeframe) pairs
   - `data_manager_backfills_triggered_total` should be low (indicating healthy data)
   - `data_manager_candle_read_fallbacks_total` should be zero post-cutover


### Responding to Queue Alerts

When the backfill request queue grows (indicated by `data_manager_backfill_queue_size`):

1. **Check orchestrator health:** `kubectl get pods -l app=backfill-orchestrator`
2. **Check `ENABLE_BACKFILLER` env var:** Ensure it is `true` in the deployment.
3. **Monitor `data_manager_backfill_queue_flushed_total`:** If this is zero while
   `data_manager_backfill_queue_size` is growing, the orchestrator may be unreachable.
4. **If queue exceeds 50:** Investigate immediately — the service may be unable to
   keep up with gap detection.
5. **After fix:** Verify queue drains: `data_manager_backfill_queue_size` should return
   to zero within the flush interval (default 60s).

### Responding to Fallback Alerts
 `python -m data_manager.maintenance.candle_readiness --json`
2. If not ready, the warmup backfill is running or needs to be triggered
3. If ready but fallbacks persist, check MongoDB health and network connectivity
4. If `CANDLE_DATABASE_TYPE` is already `mongodb`, the warm-up is incomplete

### Manual Backfill Trigger

```bash
# Trigger backfill for a specific symbol/timeframe
python -m data_manager.backfiller.orchestrator \
  --symbol BTCUSDT \
  --timeframe 1m \
  --start "2026-01-01T00:00:00Z" \
  --end "2026-01-01T06:00:00Z"
```

## Acceptance Criteria (data-manager#317)

| AC | Status | Evidence |
|----|--------|----------|
| AC1: Design document | ✅ | This document |
| AC2: Extend gap filler to MongoDB | ✅ | PR #301 (merged) |
| AC3: Continuous gap detection | ✅ | `streaming_gap_detector.py` wired into `AuditScheduler` |
| AC4: Connect analytics to backfill | ✅ | `data_manager/services/backfill_trigger.py` (NEW) |
| AC5: Fallback metric/alert | ✅ | `CANDLE_READ_FALLBACKS` counter + alert rules |
| AC6: Integration tests | ✅ | `tests/test_gap_filling_pipeline.py` (NEW) |
| AC7: Operator documentation | ✅ | This document + alerting section |
| AC8: Backfill queue (petrosa-data-manager#320) | ✅ | `data_manager/services/backfill_queue.py` + operator procedures |

## Related Tickets

- #274: MongoDB primary candle store
- #275: Cutover safety with warmup backfill
- #276: Candle consumer retention contract
- #301: Extend gap filler to fill MongoDB candles (PR, merged)
- #322: Streaming gap detector (PR)
- #320: Decouple gap detection from orchestrator requirement (this PR)
