# MySQL kline recovery

Every kline written to MongoDB is also copied to MySQL by one shared function
(`data_manager/db/repositories/kline_persistence.py`): the extractor ingest route, the backfill
orchestrator and the generic gateway insert all use it. The copy is insert-only on `(symbol, timestamp)`
and is counted in `data_manager_klines_mysql_copy_total{interval,outcome}`; an interval with no series
there has had no copy since the last restart.

The service also exposes `data_manager_klines_mysql_lag_seconds` and `data_manager_klines_mysql_stale`,
labeled by symbol and interval, from a freshness check that runs on the leader replica only. A stale value
of `1` means MongoDB is current but MySQL is missing or more than two intervals behind.

## Recover a gap

`data_manager.maintenance.copy_mongo_klines_to_mysql` takes:

- `--since <ISO time>` (required) and `--until <ISO time>` (default now);
- `--interval <5m|15m|30m|1h|1d>`, repeated to copy several intervals; every supported interval
  when omitted;
- `--batch <n>` rows per write (default 1000);
- `--dry-run` (the default) or `--apply`.

Preview first; nothing is written:

```bash
python -m data_manager.maintenance.copy_mongo_klines_to_mysql \ --since 2026-10-02T00:00:00Z --interval 1h --interval 1d --dry-run
```

After reviewing the counts, repeat the same command with `--apply`. The writes are insert-only on
`(symbol, timestamp)`: an existing row is never changed, so a rerun is safe. Each interval prints
`rows`, `inserted`, `duplicates` and `failed`; `inserted + duplicates` must equal `rows` and `failed`
must be 0. Then compare `MAX(timestamp)` in `klines_h1` and `klines_d1` with the Mongo collections.
