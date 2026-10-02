-- Operator-run additive migration for data-manager#463.
-- Capture SHOW CREATE TABLE signals before and after this file in the runbook.
-- SHOW CREATE TABLE signals;
ALTER TABLE signals
    ADD COLUMN IF NOT EXISTS signal_key VARCHAR(191) NULL,
    ADD COLUMN IF NOT EXISTS bar_open_time DATETIME NULL,
    ADD COLUMN IF NOT EXISTS bar_close_time DATETIME NULL,
    ADD COLUMN IF NOT EXISTS entry_ref_price DECIMAL(30, 12) NULL,
    ADD COLUMN IF NOT EXISTS stop_loss DECIMAL(30, 12) NULL,
    ADD COLUMN IF NOT EXISTS take_profit DECIMAL(30, 12) NULL,
    ADD COLUMN IF NOT EXISTS decision_id VARCHAR(191) NULL,
    ADD COLUMN IF NOT EXISTS signal_revision_payload_hash CHAR(64) NULL,
    ADD COLUMN IF NOT EXISTS last_rejected_payload_hash CHAR(64) NULL,
    ADD COLUMN IF NOT EXISTS signal_revision_conflicts INT UNSIGNED NOT NULL DEFAULT 0;
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
