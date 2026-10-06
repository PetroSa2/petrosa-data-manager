-- Operator-run rollback for migration 017.
-- Rollback is expected to fail if rows no longer fit in signed INT.
SET @trades_rollback_needed := (
    SELECT COUNT(*) > 0
    FROM information_schema.columns
    WHERE table_schema = DATABASE() AND table_name = 'trades'
      AND ((column_name = 'trade_id' AND data_type = 'bigint' AND is_nullable = 'NO')
        OR (column_name = 'order_id' AND data_type = 'bigint' AND is_nullable = 'YES'))
);
SET @trades_rollback_ddl := IF(
    @trades_rollback_needed,
    'ALTER TABLE trades MODIFY trade_id INT NOT NULL, MODIFY order_id INT NULL, ALGORITHM=INPLACE, LOCK=NONE',
    'SELECT 1'
);
PREPARE trades_rollback_stmt FROM @trades_rollback_ddl;
EXECUTE trades_rollback_stmt;
DEALLOCATE PREPARE trades_rollback_stmt;
