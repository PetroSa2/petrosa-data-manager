-- Migration 009 pre-snapshot checklist (read-only).
--
-- Take and verify a DBaaS snapshot of the target database before running the
-- forward migration. This file records the baseline only; it performs no DDL.
-- The snapshot is the data-recovery path if a rollback is required because the
-- PK rollback restores schema shape, not historical row values.

SELECT TABLE_NAME,
       TABLE_ROWS,
       DATA_LENGTH,
       INDEX_LENGTH,
       DATA_FREE
FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME LIKE 'klines\\_%' ESCAPE '\\'
ORDER BY TABLE_NAME;

SELECT TABLE_NAME,
       COLUMN_NAME,
       ORDINAL_POSITION,
       COLUMN_TYPE,
       IS_NULLABLE,
       COLUMN_KEY
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME LIKE 'klines\\_%' ESCAPE '\\'
ORDER BY TABLE_NAME, ORDINAL_POSITION;
