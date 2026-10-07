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

## Daily candles (klines_1d / klines_d1)

Volatility, correlation and drawdown inputs need one daily candle per UTC day, in MongoDB and in MySQL.

**Check.** The leader replica compares the last 90 complete UTC days (today's forming candle is never expected) with
both stores every hour and publishes `data_manager_klines_1d_missing_days{symbol,store}` and
`data_manager_klines_1d_completeness_ratio{symbol,store}` (`store` is `mongodb` or `mysql`). Any missing day also
logs `klines_1d_gap` with the first and last missing date. Alert on `data_manager_klines_1d_missing_days > 0`.

**Fill MongoDB from the MySQL lake** (insert-only; never changes an existing document and never writes MySQL).
Dry run first; it prints, per symbol, the days present in each store and the days MongoDB and MySQL are missing:

```bash
python -m data_manager.maintenance.fill_mongo_klines_from_mysql --since 2026-07-08
```

The days "missing_in_mongo" are what `--apply` inserts; whether the gaps are historical or still growing shows in
their dates (recent dates mean the daily ingest is still skipping days). After reviewing, repeat with `--apply`;
it prints `inserted`, `duplicates`, `failed` and `unmappable` per symbol, and exits non-zero if any day failed or
could not be mapped.

**Fill MySQL days from Binance** (the days listed as "missing_in_mysql"), for example BCHUSDT:

```bash
python -m data_manager.maintenance.historic_klines_backfill \ --symbols BCHUSDT --timeframes 1d --start 2026-07-08T00:00:00+00:00 \ --skip-early-daily --checkpoint /tmp/klines-1d-fill.json            # dry run
python -m data_manager.maintenance.historic_klines_backfill \ --symbols BCHUSDT --timeframes 1d --start 2026-07-08T00:00:00+00:00 \ --skip-early-daily --checkpoint /tmp/klines-1d-fill.json --apply
```

The backfill is insert-only on `(symbol, timestamp)` and prints `fetched`, `inserted` and `duplicates`.
`--skip-early-daily` leaves out the fixed 2021 daily range that `--timeframes 1d` otherwise also fetches.
Use a fresh `--checkpoint` file for a new range.

Long daily history for consumers (volatility, backtests) is read from MySQL through the historic range reads
(petrosa-data-manager#478).
