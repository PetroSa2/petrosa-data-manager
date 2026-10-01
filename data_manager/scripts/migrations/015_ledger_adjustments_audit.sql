-- PetroSa2/petrosa-data-manager#467
-- Operator rehearsal records SHOW CREATE TABLE positions before/after.
-- Additive, idempotent, and non-destructive migration.

CREATE TABLE IF NOT EXISTS ledger_adjustments (
  chain_position BIGINT NOT NULL AUTO_INCREMENT,
  adjustment_id VARCHAR(64) NOT NULL,
  applied_at DATETIME(6) NOT NULL,
  applied_by VARCHAR(255) NOT NULL,
  approved_by VARCHAR(255) NOT NULL,
  target_table VARCHAR(128) NOT NULL,
  target_key VARCHAR(255) NOT NULL,
  `before` JSON NOT NULL,
  `after` JSON NOT NULL,
  reason_code ENUM('phantom_superseded','status_correction','annotation') NOT NULL,
  evidence_ref VARCHAR(512) NOT NULL,
  run_mode ENUM('dry_run','apply') NOT NULL,
  dry_run_adjustment_id VARCHAR(64) NULL,
  prev_hash CHAR(64) NULL,
  row_hash CHAR(64) NOT NULL,
  UNIQUE KEY uq_ledger_adjustments_chain_position (chain_position),
  PRIMARY KEY (adjustment_id),
  INDEX idx_ledger_adjustments_target (target_table, target_key, applied_at),
  INDEX idx_ledger_adjustments_run_mode (run_mode, dry_run_adjustment_id),
  INDEX idx_ledger_adjustments_chain (applied_at, adjustment_id)
) ENGINE=InnoDB;

SET @positions_pnl_unknown_sql = IF(
  EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'positions'),
  "ALTER TABLE positions ADD COLUMN IF NOT EXISTS pnl_unknown TINYINT(1) NOT NULL DEFAULT 0",
  "SELECT 'positions table absent; pnl_unknown column skipped' AS migration_note"
);
PREPARE positions_pnl_unknown_stmt FROM @positions_pnl_unknown_sql;
EXECUTE positions_pnl_unknown_stmt;
DEALLOCATE PREPARE positions_pnl_unknown_stmt;
