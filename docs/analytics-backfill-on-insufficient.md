# Analytics Calculator Backfill on Insufficient Data

> Added in PR #328 (Closes #321). Part of unified candle gap filling pipeline #317.

## Overview

Analytics calculators (trend, volume, seasonality, volatility, deviation) detect when
insufficient candle data is available for reliable computation. When the
`ANALYTICS_BACKFILL_ON_INSUFFICIENT` env-var is enabled, they automatically trigger
a backfill request via the existing `BackfillTrigger` bridge.

## Configuration

| Env-var | Default | Description |
|---------|---------|-------------|
| `ANALYTICS_BACKFILL_ON_INSUFFICIENT` | `false` | Enable auto-trigger of backfill when calculators detect insufficient data |

To enable:

```bash
export ANALYTICS_BACKFILL_ON_INSUFFICIENT=true
```

## Prometheus Metric

**Counter**: `data_manager_analytics_backfill_triggered_total`

| Label | Description |
|-------|-------------|
| `calculator` | Calculator name (`trend`, `volume`, `seasonality`, `volatility`, `deviation`) |
| `symbol` | Trading pair (e.g., `BTCUSDT`) |
| `timeframe` | Candle timeframe (e.g., `1h`, `1d`) |

**Always incremented** when a calculator detects insufficient data. A backfill
**request** is only sent when `ANALYTICS_BACKFILL_ON_INSUFFICIENT=true` **and**
a `BackfillTrigger` is wired (via `AnalyticsScheduler`).

## Minimum Data Thresholds

| Calculator | Minimum Candles | Typical Window |
|------------|-----------------|----------------|
| Trend | 50 | 30 days |
| Volume | 5 | 24 hours |
| Seasonality | 100 | 90 days |
| Volatility | 20 | 30 days |
| Deviation | 20 | 30 days |

## Architecture

```
Calculator (e.g. TrendCalculator)
  ├── fetch candles from MongoDB
  ├── len(candles) < threshold?
  │   ├── YES → _insufficient_data()
  │   │   ├── log WARNING
  │   │   ├── increment data_manager_analytics_backfill_triggered_total
  │   │   └── if ANALYTICS_BACKFILL_ON_INSUFFICIENT=true AND backfill_trigger wired
  │   │       └── backfill_trigger.on_verdict("unhealthy", ...)
  │   └── return None
  └── NO → compute metrics → store in MongoDB
```

## Troubleshooting

### Backfill not being triggered despite `ANALYTICS_BACKFILL_ON_INSUFFICIENT=true`

1. Verify the `BackfillTrigger` is wired in `AnalyticsScheduler.__init__`:
   ```python
   scheduler = AnalyticsScheduler(db_manager, backfill_trigger=trigger)
   ```
2. Check logs for:
   ```
   ANALYTICS_BACKFILL_ON_INSUFFICIENT=true but no BackfillTrigger wired for ...
   ```

### Metric not incrementing

The metric **always** increments when insufficient data is detected, regardless of
the env-var setting. If it's not incrementing, the calculator is not being invoked
or the data fetch succeeded.

### Backfill requests not arriving at orchestrator

The `BackfillTrigger.on_verdict()` method deduplicates by cooldown (5 min default).
Rapid re-runs within the cooldown window will not produce duplicate requests.
