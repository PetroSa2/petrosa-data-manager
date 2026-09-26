-- Migration 010 for PetroSa2/petrosa_k8s#1164 (EPIC-1158 W7).
-- Measured health_metrics DATA_FREE: 114 MB; expected reclaim: approximately 114 MB.
-- OPTIMIZE TABLE is a full InnoDB rebuild and can hold a metadata lock. Run only as
-- an operator action during a maintenance window; never add it to the retention job.
-- MySQL 5.x compatible: guard the table with INFORMATION_SCHEMA before preparing it.

SELECT COUNT(*) INTO @health_metrics_exists
FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME = 'health_metrics';

SET @health_metrics_sql = IF(
    @health_metrics_exists = 1,
    'OPTIMIZE TABLE health_metrics',
    'SELECT ''health_metrics table is absent; reclaim skipped'' AS migration_note'
);
PREPARE health_metrics_stmt FROM @health_metrics_sql;
EXECUTE health_metrics_stmt;
DEALLOCATE PREPARE health_metrics_stmt;
