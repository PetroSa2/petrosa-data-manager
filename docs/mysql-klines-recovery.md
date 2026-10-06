# MySQL kline recovery

The service exposes `data_manager_klines_mysql_lag_seconds` and
`data_manager_klines_mysql_stale`, labeled by symbol and interval. A stale value of
`1` means MongoDB is current but MySQL is missing or more than two intervals behind.

Preview a recovery before writing anything:

```bash
python -m data_manager.maintenance.copy_mongo_klines_to_mysql \
  --since 2026-10-02T00:00:00Z --interval 1h --interval 1d --dry-run
```

After reviewing the counts, repeat the same command with `--apply`. The copy uses
insert-only writes and the `(symbol, timestamp)` key makes a rerun idempotent.
Verify `MAX(timestamp)` in `klines_h1` and `klines_d1` against the Mongo collections
before declaring the recovery complete.
