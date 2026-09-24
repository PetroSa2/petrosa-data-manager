-- Migration 007 pre-change snapshot query.
-- Run this against the target database before applying 007, then replace the
-- committed PENDING-OPERATOR fixture with the captured output.
SHOW CREATE TABLE audit_logs;
SHOW CREATE TABLE health_metrics;
SHOW CREATE TABLE backfill_jobs;
SHOW CREATE TABLE signals;
SHOW CREATE TABLE positions;
SHOW CREATE TABLE klines_m1;
SHOW CREATE TABLE klines_m15;
SHOW CREATE TABLE klines_m30;
SHOW CREATE TABLE klines_h1;
SHOW CREATE TABLE klines_d1;
SHOW CREATE TABLE klines_h4;

SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME
FROM INFORMATION_SCHEMA.STATISTICS
WHERE TABLE_SCHEMA = DATABASE()
  AND INDEX_NAME IN (
    'idx_klines_m5_symbol_timestamp', 'idx_klines_m30_symbol_timestamp',
    'idx_klines_h1_symbol_timestamp', 'idx_klines_d1_symbol_timestamp',
    'idx_audit_logs_dataset_timestamp', 'idx_audit_logs_symbol',
    'idx_health_metrics_dataset_timestamp', 'idx_health_metrics_symbol',
    'idx_backfill_jobs_status', 'idx_backfill_jobs_symbol', 'idx_strategy',
    'idx_symbol_period', 'idx_entry_time', 'idx_exchange',
    'idx_positions_status_entry_time', 'idx_strategy_id', 'idx_symbol',
    'idx_klines_m1_open_time', 'idx_klines_m1_timestamp',
    'idx_klines_15m_open_time', 'idx_klines_m30_open_time',
    'idx_klines_d1_timestamp', 'idx_klines_h4_open_time',
    'idx_klines_h4_timestamp', 'idx_status'
  )
ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX;
