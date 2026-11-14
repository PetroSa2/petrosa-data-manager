-- Migration 003: drop redundant open_time indexes from klines tables.
-- Ticket: PetroSa2/petrosa-data-manager#882.
-- MySQL 5.7 has no DROP INDEX IF EXISTS; guarded prepared DDL keeps reruns safe.

SET @drop_sql = IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m5' AND INDEX_NAME = 'idx_klines_m5_open_time') > 0, 'DROP INDEX idx_klines_m5_open_time', NULL);
SET @alter_sql = IF(@drop_sql IS NULL, 'SELECT ''klines_m5 already clean'' AS migration_note', CONCAT('ALTER TABLE klines_m5 ', @drop_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @drop_sql = IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m15' AND INDEX_NAME = 'idx_klines_m15_open_time') > 0, 'DROP INDEX idx_klines_m15_open_time', NULL);
SET @alter_sql = IF(@drop_sql IS NULL, 'SELECT ''klines_m15 already clean'' AS migration_note', CONCAT('ALTER TABLE klines_m15 ', @drop_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @drop_sql = IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m30' AND INDEX_NAME = 'idx_klines_m30_open_time') > 0, 'DROP INDEX idx_klines_m30_open_time', NULL);
SET @alter_sql = IF(@drop_sql IS NULL, 'SELECT ''klines_m30 already clean'' AS migration_note', CONCAT('ALTER TABLE klines_m30 ', @drop_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @drop_sql = IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_h1' AND INDEX_NAME = 'idx_klines_h1_open_time') > 0, 'DROP INDEX idx_klines_h1_open_time', NULL);
SET @alter_sql = IF(@drop_sql IS NULL, 'SELECT ''klines_h1 already clean'' AS migration_note', CONCAT('ALTER TABLE klines_h1 ', @drop_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @drop_sql = IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_d1' AND INDEX_NAME = 'idx_klines_d1_open_time') > 0, 'DROP INDEX idx_klines_d1_open_time', NULL);
SET @alter_sql = IF(@drop_sql IS NULL, 'SELECT ''klines_d1 already clean'' AS migration_note', CONCAT('ALTER TABLE klines_d1 ', @drop_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
