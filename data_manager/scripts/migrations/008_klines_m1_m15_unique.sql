-- Migration 008B: add the load-bearing UNIQUE keys for klines m1 and m15.
--
-- GUARD: run 008_klines_m1_m15_dedupe.sql first and verify BOTH queries below
-- return 0.  ADD UNIQUE fails with ERROR 1062 while either count is non-zero.
-- SELECT COUNT(*) - COUNT(DISTINCT symbol, timestamp) FROM klines_m1;
-- SELECT COUNT(*) - COUNT(DISTINCT symbol, timestamp) FROM klines_m15;
--
-- MySQL 5.7 has no conditional CREATE INDEX syntax.  The INFORMATION_SCHEMA checks
-- and prepared statements below make this migration idempotent.

SELECT COUNT(*) INTO @m1_unique_exists
FROM INFORMATION_SCHEMA.STATISTICS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME = 'klines_m1'
  AND INDEX_NAME = 'uniq_klines_m1_symbol_timestamp';

SET @m1_unique_sql = IF(
    @m1_unique_exists = 0,
    'ALTER TABLE klines_m1 ADD UNIQUE INDEX uniq_klines_m1_symbol_timestamp (symbol, timestamp), ALGORITHM=INPLACE, LOCK=NONE',
    'SELECT ''uniq_klines_m1_symbol_timestamp already exists'' AS migration_note'
);
PREPARE m1_unique_stmt FROM @m1_unique_sql;
EXECUTE m1_unique_stmt;
DEALLOCATE PREPARE m1_unique_stmt;

SELECT COUNT(*) INTO @m15_unique_exists
FROM INFORMATION_SCHEMA.STATISTICS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME = 'klines_m15'
  AND INDEX_NAME = 'uniq_klines_m15_symbol_timestamp';

SET @m15_unique_sql = IF(
    @m15_unique_exists = 0,
    'ALTER TABLE klines_m15 ADD UNIQUE INDEX uniq_klines_m15_symbol_timestamp (symbol, timestamp), ALGORITHM=INPLACE, LOCK=NONE',
    'SELECT ''uniq_klines_m15_symbol_timestamp already exists'' AS migration_note'
);
PREPARE m15_unique_stmt FROM @m15_unique_sql;
EXECUTE m15_unique_stmt;
DEALLOCATE PREPARE m15_unique_stmt;
