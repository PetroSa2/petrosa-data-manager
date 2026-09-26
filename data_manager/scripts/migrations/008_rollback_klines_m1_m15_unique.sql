-- Rollback for 008_klines_m1_m15_unique.sql.
-- The de-duplication is intentionally NOT reversible; restore from a snapshot
-- if the deleted rows are required.
-- MySQL 5.7 has no conditional DROP INDEX syntax, so INFORMATION_SCHEMA checks are used.

SELECT COUNT(*) INTO @m1_unique_exists
FROM INFORMATION_SCHEMA.STATISTICS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME = 'klines_m1'
  AND INDEX_NAME = 'uniq_klines_m1_symbol_timestamp';
SET @m1_drop_sql = IF(
    @m1_unique_exists = 1,
    'ALTER TABLE klines_m1 DROP INDEX uniq_klines_m1_symbol_timestamp, ALGORITHM=INPLACE, LOCK=NONE',
    'SELECT ''uniq_klines_m1_symbol_timestamp absent'' AS rollback_note'
);
PREPARE m1_drop_stmt FROM @m1_drop_sql;
EXECUTE m1_drop_stmt;
DEALLOCATE PREPARE m1_drop_stmt;

SELECT COUNT(*) INTO @m15_unique_exists
FROM INFORMATION_SCHEMA.STATISTICS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME = 'klines_m15'
  AND INDEX_NAME = 'uniq_klines_m15_symbol_timestamp';
SET @m15_drop_sql = IF(
    @m15_unique_exists = 1,
    'ALTER TABLE klines_m15 DROP INDEX uniq_klines_m15_symbol_timestamp, ALGORITHM=INPLACE, LOCK=NONE',
    'SELECT ''uniq_klines_m15_symbol_timestamp absent'' AS rollback_note'
);
PREPARE m15_drop_stmt FROM @m15_drop_sql;
EXECUTE m15_drop_stmt;
DEALLOCATE PREPARE m15_drop_stmt;
