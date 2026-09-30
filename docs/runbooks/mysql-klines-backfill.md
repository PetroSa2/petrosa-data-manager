# MySQL historic kline backfill

The canonical MySQL table for a timeframe is `klines_{unit}{value}`:
`5m` → `klines_m5`, `15m` → `klines_m15`, `30m` → `klines_m30`,
`1h` → `klines_h1`, and `1d` → `klines_d1`.
The `klines_{value}{unit}` family is retained as historical data and is not a
write target for data-manager.

Run the backfill from the data-manager container or a controlled maintenance
environment. It is dry-run by default and resumes completed symbol/timeframe
ranges from its checkpoint.

```bash
python -m data_manager.maintenance.historic_klines_backfill \
  --start 2026-09-24T00:00:00+00:00 \
  --end 2026-10-08T00:00:00+00:00 \
  --checkpoint /tmp/klines-backfill.json

python -m data_manager.maintenance.historic_klines_backfill \
  --start 2026-09-24T00:00:00+00:00 \
  --end 2026-10-08T00:00:00+00:00 \
  --checkpoint /tmp/klines-backfill.json --apply
```

The daily historical gap `2021-01-01` through `2021-09-17` is included when
`1d` is selected. Confirm the dry-run counters before using `--apply`; the
operation is idempotent because writes use the `(symbol, timestamp)` key.
