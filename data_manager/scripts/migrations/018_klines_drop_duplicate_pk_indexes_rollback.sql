-- Rollback for migration 018.
-- Recreates the exact unique or non-unique secondary indexes removed by the forward migration.

SET @add_sql = IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m5' AND INDEX_NAME = 'uniq_klines_m5_symbol_timestamp') = 0, 'ADD UNIQUE INDEX uniq_klines_m5_symbol_timestamp (symbol, timestamp)', NULL);
SET @alter_sql = IF(@add_sql IS NULL, 'SELECT ''klines_m5 already restored'' AS migration_note', CONCAT('ALTER TABLE klines_m5 ', @add_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @add_sql = CONCAT_WS(', ',
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m15' AND INDEX_NAME = 'uniq_klines_m15_symbol_timestamp') = 0, 'ADD UNIQUE INDEX uniq_klines_m15_symbol_timestamp (symbol, timestamp)', NULL),
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m15' AND INDEX_NAME = 'idx_klines_15m_symbol_timestamp') = 0, 'ADD INDEX idx_klines_15m_symbol_timestamp (symbol, timestamp)', NULL)
);
SET @alter_sql = IF(@add_sql = '', 'SELECT ''klines_m15 already restored'' AS migration_note', CONCAT('ALTER TABLE klines_m15 ', @add_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @add_sql = IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m30' AND INDEX_NAME = 'uniq_klines_m30_symbol_timestamp') = 0, 'ADD UNIQUE INDEX uniq_klines_m30_symbol_timestamp (symbol, timestamp)', NULL);
SET @alter_sql = IF(@add_sql IS NULL, 'SELECT ''klines_m30 already restored'' AS migration_note', CONCAT('ALTER TABLE klines_m30 ', @add_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @add_sql = IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_h1' AND INDEX_NAME = 'uniq_klines_h1_symbol_timestamp') = 0, 'ADD UNIQUE INDEX uniq_klines_h1_symbol_timestamp (symbol, timestamp)', NULL);
SET @alter_sql = IF(@add_sql IS NULL, 'SELECT ''klines_h1 already restored'' AS migration_note', CONCAT('ALTER TABLE klines_h1 ', @add_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @add_sql = IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_d1' AND INDEX_NAME = 'uniq_klines_d1_symbol_timestamp') = 0, 'ADD UNIQUE INDEX uniq_klines_d1_symbol_timestamp (symbol, timestamp)', NULL);
SET @alter_sql = IF(@add_sql IS NULL, 'SELECT ''klines_d1 already restored'' AS migration_note', CONCAT('ALTER TABLE klines_d1 ', @add_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
