-- Rollback for 020_trading_state_columns_mysql57 (PetroSa2/petrosa-data-manager#880). Operator-run.
-- Drops only the columns 020 added, in one online ALTER, and only those that exist. Rerunnable.
-- The status ENUM is NOT shrunk: rows may already hold an appended value, and removing it would rewrite them.
-- SHOW CREATE TABLE positions;
SET SESSION group_concat_max_len = 8192;
SET @positions_rollback_clauses := (
    SELECT GROUP_CONCAT(CONCAT('DROP COLUMN ', column_name) ORDER BY ordinal_position SEPARATOR ', ')
    FROM information_schema.columns
    WHERE table_schema = DATABASE() AND table_name = 'positions'
      AND column_name IN ('entry_commission', 'fee_status', 'closed_by_strategy_id', 'close_actor',
                          'idempotency_key', 'decision_id')
);
SET @positions_rollback_ddl := IF(
    @positions_rollback_clauses IS NULL,
    'SELECT 1',
    CONCAT('ALTER TABLE positions ', @positions_rollback_clauses, ', ALGORITHM=INPLACE, LOCK=NONE')
);
PREPARE positions_rollback_stmt FROM @positions_rollback_ddl;
EXECUTE positions_rollback_stmt;
DEALLOCATE PREPARE positions_rollback_stmt;
-- SHOW CREATE TABLE positions;
