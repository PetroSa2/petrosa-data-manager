-- Migration 006: Add (symbol, timestamp) composite covering indexes to audit_logs and health_metrics.
-- These covering indexes allow WHERE symbol = ? ORDER BY timestamp DESC queries to avoid filesorts.
-- MySQL 5.x compatible: uses INFORMATION_SCHEMA pre-check instead of IF NOT EXISTS.

SELECT COUNT(*) INTO @audit_idx_exists
FROM INFORMATION_SCHEMA.STATISTICS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME   = 'audit_logs'
  AND INDEX_NAME   = 'idx_audit_logs_symbol_timestamp';

SET @audit_sql = IF(
    @audit_idx_exists = 0,
    'CREATE INDEX idx_audit_logs_symbol_timestamp ON audit_logs (symbol, timestamp)',
    'SELECT ''idx_audit_logs_symbol_timestamp already exists'' AS migration_note'
);
PREPARE audit_stmt FROM @audit_sql;
EXECUTE audit_stmt;
DEALLOCATE PREPARE audit_stmt;

SELECT COUNT(*) INTO @health_idx_exists
FROM INFORMATION_SCHEMA.STATISTICS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME   = 'health_metrics'
  AND INDEX_NAME   = 'idx_health_metrics_symbol_timestamp';

SET @health_sql = IF(
    @health_idx_exists = 0,
    'CREATE INDEX idx_health_metrics_symbol_timestamp ON health_metrics (symbol, timestamp)',
    'SELECT ''idx_health_metrics_symbol_timestamp already exists'' AS migration_note'
);
PREPARE health_stmt FROM @health_sql;
EXECUTE health_stmt;
DEALLOCATE PREPARE health_stmt;
