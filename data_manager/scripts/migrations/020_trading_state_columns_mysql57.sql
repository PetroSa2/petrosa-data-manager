-- Operator-run additive migration for PetroSa2/petrosa-data-manager#880 (MySQL 5.7-safe replacement for 013's columns).
-- 013 used ADD COLUMN IF NOT EXISTS, which MySQL 5.7 rejects, so on production its columns and status values were never
-- created and the trading-state copy dropped these fields on every write.
-- Capture SHOW CREATE TABLE positions before and after this file.
-- SHOW CREATE TABLE positions;
CREATE TABLE IF NOT EXISTS schema_migrations (
    migration_id VARCHAR(128) NOT NULL PRIMARY KEY,
    applied_at DATETIME(6) NOT NULL
) ENGINE=InnoDB;
--
-- One statement, one online ALTER (ALGORITHM=INPLACE, LOCK=NONE: the server refuses rather than falling back to a
-- blocking copy). Rerunnable: columns that already exist are left out, the status values are only appended when one is
-- missing, and when nothing is missing no ALTER runs.
-- The status ENUM keeps its existing values in their existing order and appends the new ones at the end, which 5.7 does
-- in place. NOT NULL DEFAULT 'open' is kept as it is: changing nullability would rebuild the table for no benefit.
SET SESSION group_concat_max_len = 8192;
SET @positions_alter_clauses := (
    SELECT GROUP_CONCAT(wanted.ddl ORDER BY wanted.ord SEPARATOR ', ')
    FROM (
        SELECT 1 AS ord, 'c' AS kind, 'entry_commission' AS name, 'ADD COLUMN entry_commission DECIMAL(20,8) NULL' AS ddl
        UNION ALL SELECT 2, 'c', 'fee_status', 'ADD COLUMN fee_status VARCHAR(20) NULL'
        UNION ALL SELECT 3, 'c', 'closed_by_strategy_id', 'ADD COLUMN closed_by_strategy_id VARCHAR(255) NULL'
        UNION ALL SELECT 4, 'c', 'close_actor', 'ADD COLUMN close_actor VARCHAR(32) NULL'
        UNION ALL SELECT 5, 'c', 'idempotency_key', 'ADD COLUMN idempotency_key VARCHAR(128) NULL'
        UNION ALL SELECT 6, 'c', 'decision_id', 'ADD COLUMN decision_id VARCHAR(64) NULL'
        UNION ALL SELECT 7, 's', 'status',
            'MODIFY COLUMN status ENUM(''open'',''closed'',''partially_closed'',''superseded'') NOT NULL DEFAULT ''open'''
    ) AS wanted
    WHERE (wanted.kind = 'c' AND wanted.name NOT IN (
              SELECT column_name FROM information_schema.columns
              WHERE table_schema = DATABASE() AND table_name = 'positions'))
       OR (wanted.kind = 's' AND NOT EXISTS (
              SELECT 1 FROM information_schema.columns
              WHERE table_schema = DATABASE() AND table_name = 'positions' AND column_name = 'status'
                AND column_type = 'enum(''open'',''closed'',''partially_closed'',''superseded'')'))
);
SET @positions_alter_ddl := IF(
    @positions_alter_clauses IS NULL
        OR NOT EXISTS (SELECT 1 FROM information_schema.tables
                       WHERE table_schema = DATABASE() AND table_name = 'positions'),
    'SELECT 1',
    CONCAT('ALTER TABLE positions ', @positions_alter_clauses, ', ALGORITHM=INPLACE, LOCK=NONE')
);
PREPARE positions_alter_stmt FROM @positions_alter_ddl;
EXECUTE positions_alter_stmt;
DEALLOCATE PREPARE positions_alter_stmt;
-- SHOW CREATE TABLE positions;
INSERT INTO schema_migrations (migration_id, applied_at)
VALUES ('020_trading_state_columns_mysql57', UTC_TIMESTAMP(6))
ON DUPLICATE KEY UPDATE migration_id = VALUES(migration_id);
