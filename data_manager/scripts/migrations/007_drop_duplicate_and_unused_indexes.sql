-- Migration 007: Drop duplicate and unused indexes from petrosa_crypto.
-- Ticket: PetroSa2/petrosa-data-manager#344; parent PetroSa2/petrosa_k8s#1158.
-- Reason: Remove 4 duplicate klines indexes and 21 indexes proven unused or prefix-redundant.
-- Read latency is NOT expected to improve; this buys storage, ingest and write amplification.
-- MySQL 5.7 compatible: INFORMATION_SCHEMA pre-checks replace unsupported conditional drops.
-- The positions declarations in other repositories remain an explicit cross-repo follow-up.

SET @drop_sql = CONCAT_WS(', ',
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m5' AND INDEX_NAME = 'idx_klines_m5_symbol_timestamp') > 0, 'DROP INDEX idx_klines_m5_symbol_timestamp', NULL)
);
SET @alter_sql = IF(@drop_sql = '', 'SELECT ''klines_m5 already clean'' AS migration_note', CONCAT('ALTER TABLE klines_m5 ', @drop_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @drop_sql = CONCAT_WS(', ',
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m30' AND INDEX_NAME = 'idx_klines_m30_symbol_timestamp') > 0, 'DROP INDEX idx_klines_m30_symbol_timestamp', NULL),
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m30' AND INDEX_NAME = 'idx_klines_m30_open_time') > 0, 'DROP INDEX idx_klines_m30_open_time', NULL)
);
SET @alter_sql = IF(@drop_sql = '', 'SELECT ''klines_m30 already clean'' AS migration_note', CONCAT('ALTER TABLE klines_m30 ', @drop_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @drop_sql = CONCAT_WS(', ',
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_h1' AND INDEX_NAME = 'idx_klines_h1_symbol_timestamp') > 0, 'DROP INDEX idx_klines_h1_symbol_timestamp', NULL)
);
SET @alter_sql = IF(@drop_sql = '', 'SELECT ''klines_h1 already clean'' AS migration_note', CONCAT('ALTER TABLE klines_h1 ', @drop_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @drop_sql = CONCAT_WS(', ',
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_d1' AND INDEX_NAME = 'idx_klines_d1_symbol_timestamp') > 0, 'DROP INDEX idx_klines_d1_symbol_timestamp', NULL),
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_d1' AND INDEX_NAME = 'idx_klines_d1_timestamp') > 0, 'DROP INDEX idx_klines_d1_timestamp', NULL)
);
SET @alter_sql = IF(@drop_sql = '', 'SELECT ''klines_d1 already clean'' AS migration_note', CONCAT('ALTER TABLE klines_d1 ', @drop_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @drop_sql = CONCAT_WS(', ',
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'audit_logs' AND INDEX_NAME = 'idx_audit_logs_dataset_timestamp') > 0, 'DROP INDEX idx_audit_logs_dataset_timestamp', NULL),
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'audit_logs' AND INDEX_NAME = 'idx_audit_logs_symbol') > 0, 'DROP INDEX idx_audit_logs_symbol', NULL)
);
SET @alter_sql = IF(@drop_sql = '', 'SELECT ''audit_logs already clean'' AS migration_note', CONCAT('ALTER TABLE audit_logs ', @drop_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @drop_sql = CONCAT_WS(', ',
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'health_metrics' AND INDEX_NAME = 'idx_health_metrics_dataset_timestamp') > 0, 'DROP INDEX idx_health_metrics_dataset_timestamp', NULL),
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'health_metrics' AND INDEX_NAME = 'idx_health_metrics_symbol') > 0, 'DROP INDEX idx_health_metrics_symbol', NULL)
);
SET @alter_sql = IF(@drop_sql = '', 'SELECT ''health_metrics already clean'' AS migration_note', CONCAT('ALTER TABLE health_metrics ', @drop_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @drop_sql = CONCAT_WS(', ',
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'backfill_jobs' AND INDEX_NAME = 'idx_backfill_jobs_status') > 0, 'DROP INDEX idx_backfill_jobs_status', NULL),
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'backfill_jobs' AND INDEX_NAME = 'idx_backfill_jobs_symbol') > 0, 'DROP INDEX idx_backfill_jobs_symbol', NULL)
);
SET @alter_sql = IF(@drop_sql = '', 'SELECT ''backfill_jobs already clean'' AS migration_note', CONCAT('ALTER TABLE backfill_jobs ', @drop_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @drop_sql = CONCAT_WS(', ',
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'signals' AND INDEX_NAME = 'idx_strategy') > 0, 'DROP INDEX idx_strategy', NULL),
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'signals' AND INDEX_NAME = 'idx_symbol_period') > 0, 'DROP INDEX idx_symbol_period', NULL)
);
SET @alter_sql = IF(@drop_sql = '', 'SELECT ''signals already clean'' AS migration_note', CONCAT('ALTER TABLE signals ', @drop_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @drop_sql = CONCAT_WS(', ',
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'positions' AND INDEX_NAME = 'idx_entry_time') > 0, 'DROP INDEX idx_entry_time', NULL),
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'positions' AND INDEX_NAME = 'idx_exchange') > 0, 'DROP INDEX idx_exchange', NULL),
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'positions' AND INDEX_NAME = 'idx_positions_status_entry_time') > 0, 'DROP INDEX idx_positions_status_entry_time', NULL),
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'positions' AND INDEX_NAME = 'idx_strategy_id') > 0, 'DROP INDEX idx_strategy_id', NULL),
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'positions' AND INDEX_NAME = 'idx_symbol') > 0, 'DROP INDEX idx_symbol', NULL),
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'positions' AND INDEX_NAME = 'idx_status') > 0, 'DROP INDEX idx_status', NULL)
);
SET @alter_sql = IF(@drop_sql = '', 'SELECT ''positions already clean'' AS migration_note', CONCAT('ALTER TABLE positions ', @drop_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @drop_sql = CONCAT_WS(', ',
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m1' AND INDEX_NAME = 'idx_klines_m1_open_time') > 0, 'DROP INDEX idx_klines_m1_open_time', NULL),
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m1' AND INDEX_NAME = 'idx_klines_m1_timestamp') > 0, 'DROP INDEX idx_klines_m1_timestamp', NULL)
);
SET @alter_sql = IF(@drop_sql = '', 'SELECT ''klines_m1 already clean'' AS migration_note', CONCAT('ALTER TABLE klines_m1 ', @drop_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @drop_sql = IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m15' AND INDEX_NAME = 'idx_klines_15m_open_time') > 0, 'DROP INDEX idx_klines_15m_open_time', NULL);
SET @alter_sql = IF(@drop_sql IS NULL, 'SELECT ''klines_m15 already clean'' AS migration_note', CONCAT('ALTER TABLE klines_m15 ', @drop_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @drop_sql = CONCAT_WS(', ',
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_h4' AND INDEX_NAME = 'idx_klines_h4_open_time') > 0, 'DROP INDEX idx_klines_h4_open_time', NULL),
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_h4' AND INDEX_NAME = 'idx_klines_h4_timestamp') > 0, 'DROP INDEX idx_klines_h4_timestamp', NULL)
);
SET @alter_sql = IF(@drop_sql = '', 'SELECT ''klines_h4 already clean'' AS migration_note', CONCAT('ALTER TABLE klines_h4 ', @drop_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
