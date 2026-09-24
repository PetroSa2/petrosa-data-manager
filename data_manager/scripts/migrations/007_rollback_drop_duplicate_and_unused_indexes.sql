-- Rollback for migration 007.
-- Recreates the exact pre-change non-unique indexes recorded in 007_pre_snapshot_baseline.txt.
-- MySQL 5.7 compatible: INFORMATION_SCHEMA pre-checks make this script idempotent.

SET @add_sql = IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m5' AND INDEX_NAME = 'idx_klines_m5_symbol_timestamp') = 0, 'ADD INDEX idx_klines_m5_symbol_timestamp (symbol, timestamp)', NULL);
SET @alter_sql = IF(@add_sql IS NULL, 'SELECT ''klines_m5 already restored'' AS migration_note', CONCAT('ALTER TABLE klines_m5 ', @add_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @add_sql = CONCAT_WS(', ',
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m30' AND INDEX_NAME = 'idx_klines_m30_symbol_timestamp') = 0, 'ADD INDEX idx_klines_m30_symbol_timestamp (symbol, timestamp)', NULL),
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m30' AND INDEX_NAME = 'idx_klines_m30_open_time') = 0, 'ADD INDEX idx_klines_m30_open_time (open_time)', NULL)
);
SET @alter_sql = IF(@add_sql = '', 'SELECT ''klines_m30 already restored'' AS migration_note', CONCAT('ALTER TABLE klines_m30 ', @add_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @add_sql = IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_h1' AND INDEX_NAME = 'idx_klines_h1_symbol_timestamp') = 0, 'ADD INDEX idx_klines_h1_symbol_timestamp (symbol, timestamp)', NULL);
SET @alter_sql = IF(@add_sql IS NULL, 'SELECT ''klines_h1 already restored'' AS migration_note', CONCAT('ALTER TABLE klines_h1 ', @add_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @add_sql = CONCAT_WS(', ',
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_d1' AND INDEX_NAME = 'idx_klines_d1_symbol_timestamp') = 0, 'ADD INDEX idx_klines_d1_symbol_timestamp (symbol, timestamp)', NULL),
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_d1' AND INDEX_NAME = 'idx_klines_d1_timestamp') = 0, 'ADD INDEX idx_klines_d1_timestamp (timestamp)', NULL)
);
SET @alter_sql = IF(@add_sql = '', 'SELECT ''klines_d1 already restored'' AS migration_note', CONCAT('ALTER TABLE klines_d1 ', @add_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @add_sql = CONCAT_WS(', ',
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'audit_logs' AND INDEX_NAME = 'idx_audit_logs_dataset_timestamp') = 0, 'ADD INDEX idx_audit_logs_dataset_timestamp (dataset_id, timestamp)', NULL),
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'audit_logs' AND INDEX_NAME = 'idx_audit_logs_symbol') = 0, 'ADD INDEX idx_audit_logs_symbol (symbol)', NULL)
);
SET @alter_sql = IF(@add_sql = '', 'SELECT ''audit_logs already restored'' AS migration_note', CONCAT('ALTER TABLE audit_logs ', @add_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @add_sql = CONCAT_WS(', ',
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'health_metrics' AND INDEX_NAME = 'idx_health_metrics_dataset_timestamp') = 0, 'ADD INDEX idx_health_metrics_dataset_timestamp (dataset_id, timestamp)', NULL),
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'health_metrics' AND INDEX_NAME = 'idx_health_metrics_symbol') = 0, 'ADD INDEX idx_health_metrics_symbol (symbol)', NULL)
);
SET @alter_sql = IF(@add_sql = '', 'SELECT ''health_metrics already restored'' AS migration_note', CONCAT('ALTER TABLE health_metrics ', @add_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @add_sql = CONCAT_WS(', ',
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'backfill_jobs' AND INDEX_NAME = 'idx_backfill_jobs_status') = 0, 'ADD INDEX idx_backfill_jobs_status (status)', NULL),
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'backfill_jobs' AND INDEX_NAME = 'idx_backfill_jobs_symbol') = 0, 'ADD INDEX idx_backfill_jobs_symbol (symbol)', NULL)
);
SET @alter_sql = IF(@add_sql = '', 'SELECT ''backfill_jobs already restored'' AS migration_note', CONCAT('ALTER TABLE backfill_jobs ', @add_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @add_sql = CONCAT_WS(', ',
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'signals' AND INDEX_NAME = 'idx_strategy') = 0, 'ADD INDEX idx_strategy (strategy_id)', NULL),
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'signals' AND INDEX_NAME = 'idx_symbol_period') = 0, 'ADD INDEX idx_symbol_period (symbol, period)', NULL)
);
SET @alter_sql = IF(@add_sql = '', 'SELECT ''signals already restored'' AS migration_note', CONCAT('ALTER TABLE signals ', @add_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @add_sql = CONCAT_WS(', ',
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'positions' AND INDEX_NAME = 'idx_entry_time') = 0, 'ADD INDEX idx_entry_time (entry_time)', NULL),
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'positions' AND INDEX_NAME = 'idx_exchange') = 0, 'ADD INDEX idx_exchange (exchange)', NULL),
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'positions' AND INDEX_NAME = 'idx_positions_status_entry_time') = 0, 'ADD INDEX idx_positions_status_entry_time (status, entry_time)', NULL),
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'positions' AND INDEX_NAME = 'idx_strategy_id') = 0, 'ADD INDEX idx_strategy_id (strategy_id)', NULL),
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'positions' AND INDEX_NAME = 'idx_symbol') = 0, 'ADD INDEX idx_symbol (symbol)', NULL),
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'positions' AND INDEX_NAME = 'idx_status') = 0, 'ADD INDEX idx_status (status)', NULL)
);
SET @alter_sql = IF(@add_sql = '', 'SELECT ''positions already restored'' AS migration_note', CONCAT('ALTER TABLE positions ', @add_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @add_sql = CONCAT_WS(', ',
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m1' AND INDEX_NAME = 'idx_klines_m1_open_time') = 0, 'ADD INDEX idx_klines_m1_open_time (open_time)', NULL),
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m1' AND INDEX_NAME = 'idx_klines_m1_timestamp') = 0, 'ADD INDEX idx_klines_m1_timestamp (timestamp)', NULL)
);
SET @alter_sql = IF(@add_sql = '', 'SELECT ''klines_m1 already restored'' AS migration_note', CONCAT('ALTER TABLE klines_m1 ', @add_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @add_sql = IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m15' AND INDEX_NAME = 'idx_klines_15m_open_time') = 0, 'ADD INDEX idx_klines_15m_open_time (open_time)', NULL);
SET @alter_sql = IF(@add_sql IS NULL, 'SELECT ''klines_m15 already restored'' AS migration_note', CONCAT('ALTER TABLE klines_m15 ', @add_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @add_sql = CONCAT_WS(', ',
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_h4' AND INDEX_NAME = 'idx_klines_h4_open_time') = 0, 'ADD INDEX idx_klines_h4_open_time (open_time)', NULL),
    IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_h4' AND INDEX_NAME = 'idx_klines_h4_timestamp') = 0, 'ADD INDEX idx_klines_h4_timestamp (timestamp)', NULL)
);
SET @alter_sql = IF(@add_sql = '', 'SELECT ''klines_h4 already restored'' AS migration_note', CONCAT('ALTER TABLE klines_h4 ', @add_sql, ', ALGORITHM=INPLACE, LOCK=NONE'));
PREPARE stmt FROM @alter_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
