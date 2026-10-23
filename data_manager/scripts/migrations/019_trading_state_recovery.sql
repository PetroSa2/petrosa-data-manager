CREATE TABLE IF NOT EXISTS trading_state_mysql_dead_letters (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  operation VARCHAR(32) NOT NULL,
  reason VARCHAR(255) NOT NULL,
  payload JSON NOT NULL,
  created_at DATETIME(6) NOT NULL,
  INDEX idx_trading_state_dead_letters_created_at (created_at),
  INDEX idx_trading_state_dead_letters_operation (operation)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS trading_state_mysql_versions (
  copy_key VARCHAR(64) NOT NULL PRIMARY KEY,
  version BIGINT NOT NULL
) ENGINE=InnoDB;
