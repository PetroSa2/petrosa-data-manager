-- Migration 018: drop secondary indexes duplicated by the klines primary key.
-- Ticket: PetroSa2/petrosa-data-manager#527.
-- Operator-run on MySQL 5.7; the primary key remains the uniqueness constraint.

SET @drop_sql = CONCAT_WS(', ',
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m5' AND INDEX_NAME = 'uniq_klines_m5_symbol_timestamp') > 0, 'DROP INDEX uniq_klines_m5_symbol_timestamp', NULL)
);
SET @alter_sql = IF(@drop_sql = '', 'SELECT ''klines_m5 already clean'' AS migration_note', CONCAT('ALTER TABLE klines_m5 ', @drop_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @drop_sql = CONCAT_WS(', ',
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m15' AND INDEX_NAME = 'uniq_klines_m15_symbol_timestamp') > 0, 'DROP INDEX uniq_klines_m15_symbol_timestamp', NULL),
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m15' AND INDEX_NAME = 'idx_klines_15m_symbol_timestamp') > 0, 'DROP INDEX idx_klines_15m_symbol_timestamp', NULL)
);
SET @alter_sql = IF(@drop_sql = '', 'SELECT ''klines_m15 already clean'' AS migration_note', CONCAT('ALTER TABLE klines_m15 ', @drop_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @drop_sql = CONCAT_WS(', ',
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m30' AND INDEX_NAME = 'uniq_klines_m30_symbol_timestamp') > 0, 'DROP INDEX uniq_klines_m30_symbol_timestamp', NULL)
);
SET @alter_sql = IF(@drop_sql = '', 'SELECT ''klines_m30 already clean'' AS migration_note', CONCAT('ALTER TABLE klines_m30 ', @drop_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @drop_sql = CONCAT_WS(', ',
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_h1' AND INDEX_NAME = 'uniq_klines_h1_symbol_timestamp') > 0, 'DROP INDEX uniq_klines_h1_symbol_timestamp', NULL)
);
SET @alter_sql = IF(@drop_sql = '', 'SELECT ''klines_h1 already clean'' AS migration_note', CONCAT('ALTER TABLE klines_h1 ', @drop_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @drop_sql = CONCAT_WS(', ',
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_d1' AND INDEX_NAME = 'uniq_klines_d1_symbol_timestamp') > 0, 'DROP INDEX uniq_klines_d1_symbol_timestamp', NULL)
);
SET @alter_sql = IF(@drop_sql = '', 'SELECT ''klines_d1 already clean'' AS migration_note', CONCAT('ALTER TABLE klines_d1 ', @drop_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
