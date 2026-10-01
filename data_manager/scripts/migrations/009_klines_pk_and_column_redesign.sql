-- Migration 009: change klines primary keys without dropping historic columns.
--
-- The 2026-09-30 operator amendment makes this a PK-only migration. Every
-- existing column, including id and the derived/time metadata columns, stays
-- in place. id remains a secondary UNIQUE key for compatibility with any
-- references. Take and verify the DBaaS snapshot with the companion file.
-- MySQL 5.7 has no ALTER ... IF EXISTS, so each table is guarded through
-- INFORMATION_SCHEMA and safe to run again after interruption.

-- Each guarded branch performs one ALTER for that table. The ALTER is atomic,
-- so an interrupted run cannot leave the PK changed without the id UNIQUE key.

SET @pk_sql = IF(
    (SELECT COUNT(*) FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
     WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m1'
       AND CONSTRAINT_NAME = 'PRIMARY'
       AND COLUMN_NAME IN ('symbol', 'timestamp')) = 2,
    'SELECT ''klines_m1 already migrated'' AS migration_note',
    'ALTER TABLE klines_m1 DROP PRIMARY KEY, ADD PRIMARY KEY (symbol, timestamp), ADD UNIQUE KEY uq_klines_m1_id (id), ALGORITHM=INPLACE, LOCK=NONE');
PREPARE pk_stmt FROM @pk_sql; EXECUTE pk_stmt; DEALLOCATE PREPARE pk_stmt;

SET @pk_sql = IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m3' AND CONSTRAINT_NAME = 'PRIMARY' AND COLUMN_NAME IN ('symbol', 'timestamp')) = 2, 'SELECT ''klines_m3 already migrated'' AS migration_note', 'ALTER TABLE klines_m3 DROP PRIMARY KEY, ADD PRIMARY KEY (symbol, timestamp), ADD UNIQUE KEY uq_klines_m3_id (id), ALGORITHM=INPLACE, LOCK=NONE');
PREPARE pk_stmt FROM @pk_sql; EXECUTE pk_stmt; DEALLOCATE PREPARE pk_stmt;

SET @pk_sql = IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m5' AND CONSTRAINT_NAME = 'PRIMARY' AND COLUMN_NAME IN ('symbol', 'timestamp')) = 2, 'SELECT ''klines_m5 already migrated'' AS migration_note', 'ALTER TABLE klines_m5 DROP PRIMARY KEY, ADD PRIMARY KEY (symbol, timestamp), ADD UNIQUE KEY uq_klines_m5_id (id), ALGORITHM=INPLACE, LOCK=NONE');
PREPARE pk_stmt FROM @pk_sql; EXECUTE pk_stmt; DEALLOCATE PREPARE pk_stmt;

SET @pk_sql = IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m15' AND CONSTRAINT_NAME = 'PRIMARY' AND COLUMN_NAME IN ('symbol', 'timestamp')) = 2, 'SELECT ''klines_m15 already migrated'' AS migration_note', 'ALTER TABLE klines_m15 DROP PRIMARY KEY, ADD PRIMARY KEY (symbol, timestamp), ADD UNIQUE KEY uq_klines_m15_id (id), ALGORITHM=INPLACE, LOCK=NONE');
PREPARE pk_stmt FROM @pk_sql; EXECUTE pk_stmt; DEALLOCATE PREPARE pk_stmt;

SET @pk_sql = IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m30' AND CONSTRAINT_NAME = 'PRIMARY' AND COLUMN_NAME IN ('symbol', 'timestamp')) = 2, 'SELECT ''klines_m30 already migrated'' AS migration_note', 'ALTER TABLE klines_m30 DROP PRIMARY KEY, ADD PRIMARY KEY (symbol, timestamp), ADD UNIQUE KEY uq_klines_m30_id (id), ALGORITHM=INPLACE, LOCK=NONE');
PREPARE pk_stmt FROM @pk_sql; EXECUTE pk_stmt; DEALLOCATE PREPARE pk_stmt;

SET @pk_sql = IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_h1' AND CONSTRAINT_NAME = 'PRIMARY' AND COLUMN_NAME IN ('symbol', 'timestamp')) = 2, 'SELECT ''klines_h1 already migrated'' AS migration_note', 'ALTER TABLE klines_h1 DROP PRIMARY KEY, ADD PRIMARY KEY (symbol, timestamp), ADD UNIQUE KEY uq_klines_h1_id (id), ALGORITHM=INPLACE, LOCK=NONE');
PREPARE pk_stmt FROM @pk_sql; EXECUTE pk_stmt; DEALLOCATE PREPARE pk_stmt;

SET @pk_sql = IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_h2' AND CONSTRAINT_NAME = 'PRIMARY' AND COLUMN_NAME IN ('symbol', 'timestamp')) = 2, 'SELECT ''klines_h2 already migrated'' AS migration_note', 'ALTER TABLE klines_h2 DROP PRIMARY KEY, ADD PRIMARY KEY (symbol, timestamp), ADD UNIQUE KEY uq_klines_h2_id (id), ALGORITHM=INPLACE, LOCK=NONE');
PREPARE pk_stmt FROM @pk_sql; EXECUTE pk_stmt; DEALLOCATE PREPARE pk_stmt;

SET @pk_sql = IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_h4' AND CONSTRAINT_NAME = 'PRIMARY' AND COLUMN_NAME IN ('symbol', 'timestamp')) = 2, 'SELECT ''klines_h4 already migrated'' AS migration_note', 'ALTER TABLE klines_h4 DROP PRIMARY KEY, ADD PRIMARY KEY (symbol, timestamp), ADD UNIQUE KEY uq_klines_h4_id (id), ALGORITHM=INPLACE, LOCK=NONE');
PREPARE pk_stmt FROM @pk_sql; EXECUTE pk_stmt; DEALLOCATE PREPARE pk_stmt;

SET @pk_sql = IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_h6' AND CONSTRAINT_NAME = 'PRIMARY' AND COLUMN_NAME IN ('symbol', 'timestamp')) = 2, 'SELECT ''klines_h6 already migrated'' AS migration_note', 'ALTER TABLE klines_h6 DROP PRIMARY KEY, ADD PRIMARY KEY (symbol, timestamp), ADD UNIQUE KEY uq_klines_h6_id (id), ALGORITHM=INPLACE, LOCK=NONE');
PREPARE pk_stmt FROM @pk_sql; EXECUTE pk_stmt; DEALLOCATE PREPARE pk_stmt;

SET @pk_sql = IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_h8' AND CONSTRAINT_NAME = 'PRIMARY' AND COLUMN_NAME IN ('symbol', 'timestamp')) = 2, 'SELECT ''klines_h8 already migrated'' AS migration_note', 'ALTER TABLE klines_h8 DROP PRIMARY KEY, ADD PRIMARY KEY (symbol, timestamp), ADD UNIQUE KEY uq_klines_h8_id (id), ALGORITHM=INPLACE, LOCK=NONE');
PREPARE pk_stmt FROM @pk_sql; EXECUTE pk_stmt; DEALLOCATE PREPARE pk_stmt;

SET @pk_sql = IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_h12' AND CONSTRAINT_NAME = 'PRIMARY' AND COLUMN_NAME IN ('symbol', 'timestamp')) = 2, 'SELECT ''klines_h12 already migrated'' AS migration_note', 'ALTER TABLE klines_h12 DROP PRIMARY KEY, ADD PRIMARY KEY (symbol, timestamp), ADD UNIQUE KEY uq_klines_h12_id (id), ALGORITHM=INPLACE, LOCK=NONE');
PREPARE pk_stmt FROM @pk_sql; EXECUTE pk_stmt; DEALLOCATE PREPARE pk_stmt;

SET @pk_sql = IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_d1' AND CONSTRAINT_NAME = 'PRIMARY' AND COLUMN_NAME IN ('symbol', 'timestamp')) = 2, 'SELECT ''klines_d1 already migrated'' AS migration_note', 'ALTER TABLE klines_d1 DROP PRIMARY KEY, ADD PRIMARY KEY (symbol, timestamp), ADD UNIQUE KEY uq_klines_d1_id (id), ALGORITHM=INPLACE, LOCK=NONE');
PREPARE pk_stmt FROM @pk_sql; EXECUTE pk_stmt; DEALLOCATE PREPARE pk_stmt;
