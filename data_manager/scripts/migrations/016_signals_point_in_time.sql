-- Operator-run additive migration for data-manager#463.
-- Capture SHOW CREATE TABLE signals before and after this file in the runbook.
-- SHOW CREATE TABLE signals;
CREATE TABLE IF NOT EXISTS schema_migrations (
    migration_id VARCHAR(128) NOT NULL PRIMARY KEY,
    applied_at DATETIME(6) NOT NULL
) ENGINE=InnoDB;
--
-- One statement, one table rebuild. On MySQL 5.7 every ADD COLUMN rebuilds the table, and signals is large, so the
-- missing columns and the unique index are collected into a single ALTER TABLE, run in place without blocking
-- writes (ALGORITHM=INPLACE, LOCK=NONE: the server refuses to run it rather than falling back to a blocking copy).
-- Rerunnable: columns and the index that already exist are left out, and when nothing is missing no ALTER runs.
SET SESSION group_concat_max_len = 8192;
SET @signals_alter_clauses := (
    SELECT GROUP_CONCAT(wanted.ddl ORDER BY wanted.ord SEPARATOR ', ')
    FROM (
        SELECT 1 AS ord, 'c' AS kind, 'signal_key' AS name, 'ADD COLUMN signal_key VARCHAR(191) NULL' AS ddl
        UNION ALL SELECT 2, 'c', 'bar_open_time', 'ADD COLUMN bar_open_time DATETIME NULL'
        UNION ALL SELECT 3, 'c', 'bar_close_time', 'ADD COLUMN bar_close_time DATETIME NULL'
        UNION ALL SELECT 4, 'c', 'entry_ref_price', 'ADD COLUMN entry_ref_price DECIMAL(30, 12) NULL'
        UNION ALL SELECT 5, 'c', 'stop_loss', 'ADD COLUMN stop_loss DECIMAL(30, 12) NULL'
        UNION ALL SELECT 6, 'c', 'take_profit', 'ADD COLUMN take_profit DECIMAL(30, 12) NULL'
        UNION ALL SELECT 7, 'c', 'decision_id', 'ADD COLUMN decision_id VARCHAR(191) NULL'
        UNION ALL SELECT 8, 'c', 'signal_revision_payload_hash', 'ADD COLUMN signal_revision_payload_hash CHAR(64) NULL'
        UNION ALL SELECT 9, 'c', 'last_rejected_payload_hash', 'ADD COLUMN last_rejected_payload_hash CHAR(64) NULL'
        UNION ALL SELECT 10, 'c', 'signal_revision_conflicts', 'ADD COLUMN signal_revision_conflicts INT UNSIGNED NOT NULL DEFAULT 0'
        UNION ALL SELECT 11, 'i', 'uq_signals_signal_key', 'ADD UNIQUE INDEX uq_signals_signal_key (signal_key)'
    ) AS wanted
    WHERE (wanted.kind = 'c' AND wanted.name NOT IN (
              SELECT column_name FROM information_schema.columns
              WHERE table_schema = DATABASE() AND table_name = 'signals'))
       OR (wanted.kind = 'i' AND wanted.name NOT IN (
              SELECT index_name FROM information_schema.statistics
              WHERE table_schema = DATABASE() AND table_name = 'signals'))
);
SET @signals_alter_ddl := IF(
    @signals_alter_clauses IS NULL,
    'SELECT 1',
    CONCAT('ALTER TABLE signals ', @signals_alter_clauses, ', ALGORITHM=INPLACE, LOCK=NONE')
);
PREPARE signals_alter_stmt FROM @signals_alter_ddl;
EXECUTE signals_alter_stmt;
DEALLOCATE PREPARE signals_alter_stmt;
-- SHOW CREATE TABLE signals;
INSERT INTO schema_migrations (migration_id, applied_at)
VALUES ('016_signals_point_in_time', UTC_TIMESTAMP(6))
ON DUPLICATE KEY UPDATE migration_id = VALUES(migration_id);
