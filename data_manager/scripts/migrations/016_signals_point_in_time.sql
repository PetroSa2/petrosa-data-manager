-- Operator-run additive migration for data-manager#463.
-- Capture SHOW CREATE TABLE signals before and after this file in the runbook.
-- SHOW CREATE TABLE signals;
SET @signals_column_ddl := IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'signals' AND column_name = 'signal_key') = 0,
    'ALTER TABLE signals ADD COLUMN signal_key VARCHAR(191) NULL',
    'SELECT 1'
);
PREPARE signals_column_stmt FROM @signals_column_ddl;
EXECUTE signals_column_stmt;
DEALLOCATE PREPARE signals_column_stmt;

SET @signals_column_ddl := IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'signals' AND column_name = 'bar_open_time') = 0,
    'ALTER TABLE signals ADD COLUMN bar_open_time DATETIME NULL',
    'SELECT 1'
);
PREPARE signals_column_stmt FROM @signals_column_ddl;
EXECUTE signals_column_stmt;
DEALLOCATE PREPARE signals_column_stmt;

SET @signals_column_ddl := IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'signals' AND column_name = 'bar_close_time') = 0,
    'ALTER TABLE signals ADD COLUMN bar_close_time DATETIME NULL',
    'SELECT 1'
);
PREPARE signals_column_stmt FROM @signals_column_ddl;
EXECUTE signals_column_stmt;
DEALLOCATE PREPARE signals_column_stmt;

SET @signals_column_ddl := IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'signals' AND column_name = 'entry_ref_price') = 0,
    'ALTER TABLE signals ADD COLUMN entry_ref_price DECIMAL(30, 12) NULL',
    'SELECT 1'
);
PREPARE signals_column_stmt FROM @signals_column_ddl;
EXECUTE signals_column_stmt;
DEALLOCATE PREPARE signals_column_stmt;

SET @signals_column_ddl := IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'signals' AND column_name = 'stop_loss') = 0,
    'ALTER TABLE signals ADD COLUMN stop_loss DECIMAL(30, 12) NULL',
    'SELECT 1'
);
PREPARE signals_column_stmt FROM @signals_column_ddl;
EXECUTE signals_column_stmt;
DEALLOCATE PREPARE signals_column_stmt;

SET @signals_column_ddl := IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'signals' AND column_name = 'take_profit') = 0,
    'ALTER TABLE signals ADD COLUMN take_profit DECIMAL(30, 12) NULL',
    'SELECT 1'
);
PREPARE signals_column_stmt FROM @signals_column_ddl;
EXECUTE signals_column_stmt;
DEALLOCATE PREPARE signals_column_stmt;

SET @signals_column_ddl := IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'signals' AND column_name = 'decision_id') = 0,
    'ALTER TABLE signals ADD COLUMN decision_id VARCHAR(191) NULL',
    'SELECT 1'
);
PREPARE signals_column_stmt FROM @signals_column_ddl;
EXECUTE signals_column_stmt;
DEALLOCATE PREPARE signals_column_stmt;

SET @signals_column_ddl := IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'signals' AND column_name = 'signal_revision_payload_hash') = 0,
    'ALTER TABLE signals ADD COLUMN signal_revision_payload_hash CHAR(64) NULL',
    'SELECT 1'
);
PREPARE signals_column_stmt FROM @signals_column_ddl;
EXECUTE signals_column_stmt;
DEALLOCATE PREPARE signals_column_stmt;

SET @signals_column_ddl := IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'signals' AND column_name = 'last_rejected_payload_hash') = 0,
    'ALTER TABLE signals ADD COLUMN last_rejected_payload_hash CHAR(64) NULL',
    'SELECT 1'
);
PREPARE signals_column_stmt FROM @signals_column_ddl;
EXECUTE signals_column_stmt;
DEALLOCATE PREPARE signals_column_stmt;

SET @signals_column_ddl := IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'signals' AND column_name = 'signal_revision_conflicts') = 0,
    'ALTER TABLE signals ADD COLUMN signal_revision_conflicts INT UNSIGNED NOT NULL DEFAULT 0',
    'SELECT 1'
);
PREPARE signals_column_stmt FROM @signals_column_ddl;
EXECUTE signals_column_stmt;
DEALLOCATE PREPARE signals_column_stmt;
-- Build the unique index only after all columns exist, and make reruns safe.
SET @signals_key_index_exists := (
    SELECT COUNT(*)
    FROM information_schema.statistics
    WHERE table_schema = DATABASE()
      AND table_name = 'signals'
      AND index_name = 'uq_signals_signal_key'
);
SET @signals_key_index_ddl := IF(
    @signals_key_index_exists = 0,
    'CREATE UNIQUE INDEX uq_signals_signal_key ON signals (signal_key)',
    'SELECT 1'
);
PREPARE signals_key_index_stmt FROM @signals_key_index_ddl;
EXECUTE signals_key_index_stmt;
DEALLOCATE PREPARE signals_key_index_stmt;
-- SHOW CREATE TABLE signals;
