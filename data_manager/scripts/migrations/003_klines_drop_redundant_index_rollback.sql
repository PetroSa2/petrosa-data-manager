-- Rollback for migration 003.
-- MySQL 5.7 compatible: INFORMATION_SCHEMA guards make index recreation idempotent.

SET @add_sql = IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m5' AND INDEX_NAME = 'idx_klines_m5_open_time') = 0, 'ADD INDEX idx_klines_m5_open_time (open_time)', NULL);
SET @alter_sql = IF(@add_sql IS NULL, 'SELECT ''klines_m5 already restored'' AS migration_note', CONCAT('ALTER TABLE klines_m5 ', @add_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @add_sql = IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m15' AND INDEX_NAME = 'idx_klines_m15_open_time') = 0, 'ADD INDEX idx_klines_m15_open_time (open_time)', NULL);
SET @alter_sql = IF(@add_sql IS NULL, 'SELECT ''klines_m15 already restored'' AS migration_note', CONCAT('ALTER TABLE klines_m15 ', @add_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @add_sql = IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m30' AND INDEX_NAME = 'idx_klines_m30_open_time') = 0, 'ADD INDEX idx_klines_m30_open_time (open_time)', NULL);
SET @alter_sql = IF(@add_sql IS NULL, 'SELECT ''klines_m30 already restored'' AS migration_note', CONCAT('ALTER TABLE klines_m30 ', @add_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @add_sql = IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_h1' AND INDEX_NAME = 'idx_klines_h1_open_time') = 0, 'ADD INDEX idx_klines_h1_open_time (open_time)', NULL);
SET @alter_sql = IF(@add_sql IS NULL, 'SELECT ''klines_h1 already restored'' AS migration_note', CONCAT('ALTER TABLE klines_h1 ', @add_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @add_sql = IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_d1' AND INDEX_NAME = 'idx_klines_d1_open_time') = 0, 'ADD INDEX idx_klines_d1_open_time (open_time)', NULL);
SET @alter_sql = IF(@add_sql IS NULL, 'SELECT ''klines_d1 already restored'' AS migration_note', CONCAT('ALTER TABLE klines_d1 ', @add_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
