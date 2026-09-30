-- PetroSa2/petrosa-data-manager#464. Operator-only, additive and idempotent.
-- Capture SHOW CREATE TABLE positions and execution_events before applying.
SET @positions_status_sql = IF(
  EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'positions'),
  "ALTER TABLE positions MODIFY status ENUM('open','partially_closed','closed','superseded') NULL",
  "SELECT 'positions table absent; status change skipped' AS migration_note"
);
PREPARE positions_status_stmt FROM @positions_status_sql;
EXECUTE positions_status_stmt;
DEALLOCATE PREPARE positions_status_stmt;

SET @positions_columns_sql = IF(
  EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'positions'),
  "ALTER TABLE positions ADD COLUMN IF NOT EXISTS entry_commission DECIMAL(20,8) NULL, ADD COLUMN IF NOT EXISTS pnl_unknown TINYINT(1) NOT NULL DEFAULT 0, ADD COLUMN IF NOT EXISTS fee_status VARCHAR(20) NULL, ADD COLUMN IF NOT EXISTS closed_by_strategy_id VARCHAR(255) NULL",
  "SELECT 'positions table absent; columns skipped' AS migration_note"
);
PREPARE positions_columns_stmt FROM @positions_columns_sql;
EXECUTE positions_columns_stmt;
DEALLOCATE PREPARE positions_columns_stmt;

SET @events_columns_sql = IF(
  EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'execution_events'),
  "ALTER TABLE execution_events ADD COLUMN IF NOT EXISTS fee_status VARCHAR(20) NULL, ADD COLUMN IF NOT EXISTS closed_by_strategy_id VARCHAR(255) NULL",
  "SELECT 'execution_events table absent; columns skipped' AS migration_note"
);
PREPARE events_columns_stmt FROM @events_columns_sql;
EXECUTE events_columns_stmt;
DEALLOCATE PREPARE events_columns_stmt;

CREATE TABLE IF NOT EXISTS trading_state_mysql_dead_letters (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  operation VARCHAR(32) NOT NULL,
  reason VARCHAR(255) NOT NULL,
  payload JSON NOT NULL,
  created_at DATETIME(6) NOT NULL,
  INDEX idx_trading_state_dead_letters_created_at (created_at),
  INDEX idx_trading_state_dead_letters_operation (operation)
) ENGINE=InnoDB;
