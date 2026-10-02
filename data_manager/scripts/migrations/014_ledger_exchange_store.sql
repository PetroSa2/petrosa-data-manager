CREATE TABLE IF NOT EXISTS ledger_exchange_day_revision (
  day DATE NOT NULL, revision INT NOT NULL, wallet_balance DECIMAL(20,8) NOT NULL,
  balance_as_of_ms BIGINT NULL, income_after_day_end JSON NOT NULL, is_final TINYINT(1) NOT NULL,
  row_count INT NOT NULL, first_income_time_ms BIGINT NULL, last_income_time_ms BIGINT NULL,
  payload_hash CHAR(64) NOT NULL, source_run_id VARCHAR(255) NOT NULL, received_at DATETIME(6) NOT NULL,
  PRIMARY KEY (day, revision), UNIQUE KEY uq_ledger_day_payload (day, payload_hash)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS ledger_exchange_daily (
  day DATE NOT NULL, revision INT NOT NULL, symbol VARCHAR(64) NOT NULL, asset VARCHAR(32) NOT NULL,
  realized_pnl DECIMAL(20,8) NOT NULL DEFAULT 0, commission DECIMAL(20,8) NOT NULL DEFAULT 0,
  funding_fee DECIMAL(20,8) NOT NULL DEFAULT 0, transfer DECIMAL(20,8) NOT NULL DEFAULT 0,
  commission_rebate DECIMAL(20,8) NOT NULL DEFAULT 0, api_rebate DECIMAL(20,8) NOT NULL DEFAULT 0,
  insurance_clear DECIMAL(20,8) NOT NULL DEFAULT 0, auto_exchange DECIMAL(20,8) NOT NULL DEFAULT 0,
  other_unnamed DECIMAL(20,8) NOT NULL DEFAULT 0, income_by_type JSON NOT NULL,
  PRIMARY KEY (day, revision, symbol, asset), FOREIGN KEY (day, revision) REFERENCES ledger_exchange_day_revision(day, revision)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS ledger_exchange_positions_snapshot (
  as_of_ms BIGINT NOT NULL PRIMARY KEY, position_count INT NOT NULL, source_run_id VARCHAR(255) NOT NULL,
  payload_hash CHAR(64) NOT NULL, received_at DATETIME(6) NOT NULL
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS ledger_exchange_positions (
  as_of_ms BIGINT NOT NULL, symbol VARCHAR(64) NOT NULL, position_side VARCHAR(16) NOT NULL,
  quantity DECIMAL(20,8) NOT NULL, entry_price DECIMAL(20,8) NOT NULL, mark_price DECIMAL(20,8) NOT NULL,
  unrealized_pnl DECIMAL(20,8) NOT NULL, PRIMARY KEY (as_of_ms, symbol, position_side),
  FOREIGN KEY (as_of_ms) REFERENCES ledger_exchange_positions_snapshot(as_of_ms)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS ledger_exchange_metrics (metric VARCHAR(128) PRIMARY KEY, value BIGINT NOT NULL DEFAULT 0) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS schema_migrations (
  migration_id VARCHAR(128) NOT NULL PRIMARY KEY,
  applied_at DATETIME(6) NOT NULL
) ENGINE=InnoDB;
INSERT INTO schema_migrations (migration_id, applied_at)
VALUES ('014_ledger_exchange_store', UTC_TIMESTAMP(6))
ON DUPLICATE KEY UPDATE migration_id = VALUES(migration_id);
