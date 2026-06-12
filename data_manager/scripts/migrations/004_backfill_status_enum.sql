-- Migration 004: convert backfill_jobs.status from VARCHAR(20) to ENUM
-- Pre-check (MANDATORY before running ALTER):
--   SELECT DISTINCT status, COUNT(*) FROM backfill_jobs GROUP BY status;
-- Expected values: subset of 'pending', 'running', 'completed', 'failed'.
-- If unexpected values exist, normalize them before running this migration.
ALTER TABLE backfill_jobs
  MODIFY COLUMN status ENUM('pending', 'running', 'completed', 'failed') NOT NULL;
