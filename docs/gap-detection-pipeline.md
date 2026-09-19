# Unified Candle Gap Detection-to-Filling Pipeline

**Issue:** [PetroSa2/petrosa-data-manager#317](https://github.com/PetroSa2/petrosa-data-manager/issues/317)
**Status:** shipped
**Created:** 2026-09-19
**Components:** `data_manager/auditor/`, `data_manager/services/backfill_trigger.py`, `data_manager/maintenance/candle_readiness.py`, `data_manager/backfiller/orchestrator.py`

---

## 1. Problem

The candle filling system had architectural gaps that left MongoDB `candles_*` collections with insufficient data for strategy execution. Evidence: 1,196 "insufficient candles" warnings in 4 hours across all 10 symbols, with candle counts as low as 0-2 candles (need 50-265 for strategies).

Trading strategies cannot fire when analytics calculators return `None` due to insufficient candles. The degradation was silent with no automatic recovery.

## 2. Architecture

The unified pipeline has four layers:

```
┌──────────────────────────────────────────────────────────┐
│  Layer 1: Continuous Gap Detection                        │
│  ┌─────────────────────┐  ┌──────────────────────────┐  │
│  │ StreamingDetector   │  │ Batch GapDetector        │  │
│  │ (NATS kline events) │  │ (15-min audit cycle)     │  │
│  └────────┬────────────┘  └──────────┬───────────────┘  │
│           │                          │                   │
│           └──────────┬───────────────┘                   │
│                      ▼                                    │
│           triggers backfill requests                      │
├──────────────────────────────────────────────────────────┤
│  Layer 2: Analytics Bridge                              │
│  ┌──────────────────────────────────────────────────┐    │
│  │ BackfillTrigger (on_verdict)                     │    │
│  │ Parses evaluator verdicts → triggers backfill    │    │
│  │ Dedup via 5-min cooldown per verdict key         │    │
│  └──────────────────────────────────────────────────┘    │
├──────────────────────────────────────────────────────────┤
│  Layer 3: Backfill Execution                            │
│  ┌──────────────────────────────────────────────────┐    │
│  │ BackfillOrchestrator                              │    │
│  │ 1. Fetch candles from Binance API                 │    │
│  │ 2. Chunk into 1000-candle batches                 │    │
│  │ 3. Insert via CandleRepository (dual MySQL+Mongo) │    │
│  └──────────────────────────────────────────────────┘    │
├──────────────────────────────────────────────────────────┤
│  Layer 4: Readiness Gate & Observability                │
│  ┌─────────────────────┐  ┌──────────────────────────┐  │
│  │ CandleReadiness     │  │ Prometheus metrics:      │  │
│  │ (depth + freshness) │  │ data_manager_gaps_       │  │
│  │ Fail-closed gate    │  │   detected_total         │  │
│  └─────────────────────┘  │ data_manager_backfills_  │  │
│                           │   triggered_total        │  │
│                           │ data_manager_fallbacks_  │  │
│                           │   total{primary,op}      │  │
│                           └──────────────────────────┘  │
└──────────────────────────────────────────────────────────┘
```

### 2.1 Data Flow

1. **Streaming path (real-time):**
   - NATS kline event arrives → `StreamingGapDetector._on_kline_event()`
   - Compares received candle timestamp against expected next timestamp
   - If gap > `STREAMING_GAP_TOLERANCE_INTERVALS * interval` → triggers backfill
   - Dedup via 30s cooldown per (symbol, timeframe)

2. **Batch path (periodic):**
   - `AuditScheduler.run_audit_cycle()` runs every `AUDIT_INTERVAL` (15 min)
   - Queries `GapDetector.detect_gaps()` for all (symbol, timeframe) pairs
   - If `ENABLE_AUTO_BACKFILL` and gap > `MIN_AUTO_BACKFILL_GAP` → triggers backfill

3. **Analytics bridge (verdict-driven):**
   - `GapDetectorEvaluator.cycle_sample()` produces verdict from audit cycle
   - Verdict published on `evaluator.data-manager.verdict`
   - `BackfillTrigger.on_verdict()` consumes verdict
   - Parses worst gap from reason string → triggers backfill

4. **Readiness-triggered backfill:**
   - `BackfillTrigger._trigger_from_readiness_check()` calls `candle_readiness.evaluate_readiness()`
   - For each not-ready collection → triggers backfill request

### 2.2 Gap Detection Parameters

| Parameter | Value | Meaning |
|---|---|---|
| `STREAMING_GAP_TOLERANCE_INTERVALS` | 2 | Gap declared when time between candles exceeds 2 intervals |
| `MIN_GAP_DURATION_SECONDS` | 180 | Minimum gap duration to trigger backfill (3 min) |
| `MAX_BACKFILL_WINDOW_SECONDS` | 86400 | Max backfill window per request (24 hours) |
| `MIN_DATA_AGE_SECONDS` | 300 | Minimum data age to skip backfill (5 min) |
| `AUDIT_INTERVAL` | 900s | Batch gap detection cycle (15 min) |
| `MIN_AUTO_BACKFILL_GAP` | 3600s | Minimum gap for auto-backfill in batch detector (1 hour) |

### 2.3 Backfill Window Capping

When the streaming detector fires, the backfill window is:

```
gap_start = expected_next_timestamp
gap_end = min(received_candle_time, gap_start + 24h)
```

This prevents requesting months of backfill if the service was down for a long time.

### 2.4 Dedup Strategy

Both detectors use dedup to avoid excessive backfill requests:

- **Streaming:** 30-second cooldown per (symbol, timeframe) key, using `time.monotonic()`
- **Analytics bridge:** 5-minute cooldown per verdict key (`verdict:reason[:100]`), using `datetime.now(UTC)`

### 2.5 Fallback Mechanism

The candle read path has a kill-switch (`CANDLE_READ_FALLBACK_ENABLED`):

- When `CANDLE_READ_FALLBACK_ENABLED=true` and the primary (MongoDB) read fails, the system falls back to MySQL
- The fallback is tracked via `data_manager_candle_read_fallbacks_total{primary,operation}` counter
- This is a **safety net**, not a substitute for gap filling

## 3. Acceptance Criteria Status

| # | Criterion | Status | Evidence |
|---|---|---|---|
| AC1 | Design document | ✅ | This document |
| AC2 | Extend gap filler to target both MySQL and MongoDB | ✅ | PR #301 (binance-data-extractor) |
| AC3 | Implement continuous gap detection | ✅ | `streaming_gap_detector.py` + wired in `AuditScheduler` |
| AC4 | Connect analytics calculators to trigger backfill | ✅ | `backfill_trigger.py` wired in `main.py` |
| AC5 | Add metric/alert when fallback is engaged | ✅ | `CANDLE_READ_FALLBACKS` counter in `candle_repository.py` |
| AC6 | Integration tests | ✅ | `test_gap_filling_pipeline.py` + `test_streaming_gap_detector.py` |
| AC7 | Documentation update for operators | ✅ | This document + `GAP_FILLER_MONGODB_CANDLES.md` |

## 4. Configuration

All configuration is via environment variables:

| Env var | Default | Meaning |
|---|---|---|
| `ENABLE_STREAMING_GAP_DETECTION` | `true` | Master switch for streaming gap detector |
| `ENABLE_AUTO_BACKFILL` | `false` | Master switch for auto-backfill in batch detector |
| `MIN_AUTO_BACKFILL_GAP` | `3600` | Minimum gap (seconds) for auto-backfill |
| `CANDLE_READ_FALLBACK_ENABLED` | `true` | Kill-switch for MySQL fallback |
| `CANDLE_WARMUP_MIN_CANDLES` | `400` | Required depth for readiness gate |
| `CANDLE_WARMUP_FRESHNESS_INTERVALS` | `3` | Required freshness as multiple of timeframe |
| `CANDLE_DUAL_WRITE_ENABLED` | `false` | Enable dual-write in gap filler |

## 5. Prometheus Metrics

### Gap Detection

| Metric | Type | Labels | Description |
|---|---|---|---|
| `data_manager_gaps_detected_total` | counter | `symbol`, `timeframe`, `source` | Gaps detected by streaming detector |
| `data_manager_gap_detection_latency_seconds` | histogram | `symbol`, `timeframe` | Time from gap start to detection |
| `data_manager_audit_gaps_detected_total` | counter | `symbol`, `timeframe` | Gaps detected in batch cycle |
| `data_manager_audit_health_score` | gauge | `symbol`, `timeframe` | Dataset health score (0-100) |

### Backfill

| Metric | Type | Labels | Description |
|---|---|---|---|
| `data_manager_backfills_triggered_total` | counter | `symbol`, `timeframe`, `source` | Backfills triggered by streaming detector |
| `data_manager_trigger_backfills_total` | counter | `trigger_source`, `symbol`, `timeframe` | Backfills triggered by analytics bridge |
| `data_manager_backfill_request_latency_seconds` | histogram | `trigger_source` | Time from verdict to backfill request |
| `data_manager_auto_backfill_triggered_total` | counter | `symbol`, `timeframe`, `reason` | Auto-triggered backfills from gap detector |

### Fallback

| Metric | Type | Labels | Description |
|---|---|---|---|
| `data_manager_candle_read_fallbacks_total` | counter | `primary`, `operation` | Fallback engagements (primary=source, operation=type) |

### Streaming Detector Status

| Metric | Type | Labels | Description |
|---|---|---|---|
| `data_manager_streaming_detector_status` | gauge | `symbol`, `timeframe` | Detector status (1=active, 0=inactive) |
| `data_manager_last_seen_candle_timestamp` | gauge | `symbol`, `timeframe` | Last seen candle timestamp |
| `data_manager_streaming_detector_active` | gauge | (none) | Overall streaming detector active status |

## 6. Related Issues

- [#274](https://github.com/PetroSa2/petrosa-data-manager/issues/274) — MongoDB primary candle store
- [#275](https://github.com/PetroSa2/petrosa-data-manager/issues/275) — Candle warm-up backfill, readiness gate
- [#276](https://github.com/PetroSa2/petrosa-data-manager/issues/276) — Candle consumer retention contract
- [#300](https://github.com/PetroSa2/petrosa-binance-data-extractor/issues/300) — Gap filler MongoDB dual-write
- [#322](https://github.com/PetroSa2/petrosa-data-manager/issues/322) — Streaming gap detector
