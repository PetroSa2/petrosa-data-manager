-- Operator-run migration for data-manager#524.
-- Capture SHOW CREATE TABLE trades before and after this file in the runbook.
CREATE TABLE IF NOT EXISTS schema_migrations (
    migration_id VARCHAR(128) NOT NULL PRIMARY KEY,
    applied_at DATETIME(6) NOT NULL
) ENGINE=InnoDB;

-- MySQL 5.7-safe and rerunnable: both changes are one online ALTER.
SET @trades_alter_clauses := (
    SELECT GROUP_CONCAT(wanted.ddl ORDER BY wanted.ord SEPARATOR ', ')
    FROM (
        SELECT 1 AS ord, 'trade_id' AS name,
               'MODIFY trade_id BIGINT NOT NULL' AS ddl
        UNION ALL SELECT 2, 'order_id', 'MODIFY order_id BIGINT NULL'
    ) AS wanted
    WHERE EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = DATABASE() AND table_name = 'trades'
    )
    AND (
        (wanted.name = 'trade_id' AND EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = DATABASE() AND table_name = 'trades'
              AND column_name = 'trade_id'
              AND (data_type <> 'bigint' OR is_nullable <> 'NO')
        ))
        OR (wanted.name = 'order_id' AND EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = DATABASE() AND table_name = 'trades'
              AND column_name = 'order_id'
              AND (data_type <> 'bigint' OR is_nullable <> 'YES')
        ))
    )
);
SET @trades_alter_ddl := IF(
    @trades_alter_clauses IS NULL,
    'SELECT 1',
    CONCAT('ALTER TABLE trades ', @trades_alter_clauses, ', ALGORITHM=INPLACE, LOCK=NONE')
);
PREPARE trades_alter_stmt FROM @trades_alter_ddl;
EXECUTE trades_alter_stmt;
DEALLOCATE PREPARE trades_alter_stmt;

INSERT INTO schema_migrations (migration_id, applied_at)
VALUES ('017_trades_bigint', UTC_TIMESTAMP(6))
ON DUPLICATE KEY UPDATE migration_id = VALUES(migration_id);
