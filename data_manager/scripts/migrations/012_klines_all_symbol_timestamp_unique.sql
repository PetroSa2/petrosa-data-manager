-- Make every live historic kline table idempotent on (symbol, timestamp).
-- Run the repository's duplicate cleanup before applying this migration.
SET @sql = IF(
  (SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS
   WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m5'
     AND INDEX_NAME = 'uniq_klines_m5_symbol_timestamp') = 0,
  'ALTER TABLE klines_m5 ADD UNIQUE INDEX uniq_klines_m5_symbol_timestamp (symbol, timestamp)',
  'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = IF(
  (SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS
   WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m15'
     AND INDEX_NAME = 'uniq_klines_m15_symbol_timestamp') = 0,
  'ALTER TABLE klines_m15 ADD UNIQUE INDEX uniq_klines_m15_symbol_timestamp (symbol, timestamp)',
  'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = IF(
  (SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS
   WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m30'
     AND INDEX_NAME = 'uniq_klines_m30_symbol_timestamp') = 0,
  'ALTER TABLE klines_m30 ADD UNIQUE INDEX uniq_klines_m30_symbol_timestamp (symbol, timestamp)',
  'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = IF(
  (SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS
   WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_h1'
     AND INDEX_NAME = 'uniq_klines_h1_symbol_timestamp') = 0,
  'ALTER TABLE klines_h1 ADD UNIQUE INDEX uniq_klines_h1_symbol_timestamp (symbol, timestamp)',
  'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = IF(
  (SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS
   WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_d1'
     AND INDEX_NAME = 'uniq_klines_d1_symbol_timestamp') = 0,
  'ALTER TABLE klines_d1 ADD UNIQUE INDEX uniq_klines_d1_symbol_timestamp (symbol, timestamp)',
  'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
