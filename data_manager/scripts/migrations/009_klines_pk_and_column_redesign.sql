-- Migration 009: change klines primary keys without dropping historic columns.
--
-- Each table is checked through INFORMATION_SCHEMA before a statement is
-- prepared. Existing columns remain in place and id remains a secondary
-- UNIQUE key. The DBaaS snapshot is the data recovery path.

SET @table_name = 'klines_m1';
SET @pk_sql = IF(
    NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES
               WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = @table_name
                 AND TABLE_TYPE = 'BASE TABLE'),
    CONCAT('SELECT ''', @table_name, ' absent'' AS migration_note'),
    IF(
        (SELECT COUNT(*) FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
         WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = @table_name
           AND CONSTRAINT_NAME = 'PRIMARY'
           AND COLUMN_NAME IN ('symbol', 'timestamp')) = 2
        AND EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS
                    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = @table_name
                      AND INDEX_NAME = CONCAT('uq_', @table_name, '_id')
                      AND NON_UNIQUE = 0 AND COLUMN_NAME = 'id'),
        CONCAT('SELECT ''', @table_name, ' already migrated'' AS migration_note'),
        CONCAT('ALTER TABLE ', @table_name,
               ' DROP PRIMARY KEY, ADD PRIMARY KEY (symbol, timestamp),',
               ' ADD UNIQUE KEY uq_', @table_name,
               '_id (id), ALGORITHM=INPLACE, LOCK=NONE')));
PREPARE pk_stmt FROM @pk_sql; EXECUTE pk_stmt; DEALLOCATE PREPARE pk_stmt;

SET @table_name = 'klines_m3';
SET @pk_sql = IF(NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = @table_name AND TABLE_TYPE = 'BASE TABLE'), CONCAT('SELECT ''', @table_name, ' absent'' AS migration_note'), IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = @table_name AND CONSTRAINT_NAME = 'PRIMARY' AND COLUMN_NAME IN ('symbol', 'timestamp')) = 2 AND EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = @table_name AND INDEX_NAME = CONCAT('uq_', @table_name, '_id') AND NON_UNIQUE = 0 AND COLUMN_NAME = 'id'), CONCAT('SELECT ''', @table_name, ' already migrated'' AS migration_note'), CONCAT('ALTER TABLE ', @table_name, ' DROP PRIMARY KEY, ADD PRIMARY KEY (symbol, timestamp), ADD UNIQUE KEY uq_', @table_name, '_id (id), ALGORITHM=INPLACE, LOCK=NONE')));
PREPARE pk_stmt FROM @pk_sql; EXECUTE pk_stmt; DEALLOCATE PREPARE pk_stmt;

SET @table_name = 'klines_m5';
SET @pk_sql = IF(NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = @table_name AND TABLE_TYPE = 'BASE TABLE'), CONCAT('SELECT ''', @table_name, ' absent'' AS migration_note'), IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = @table_name AND CONSTRAINT_NAME = 'PRIMARY' AND COLUMN_NAME IN ('symbol', 'timestamp')) = 2 AND EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = @table_name AND INDEX_NAME = CONCAT('uq_', @table_name, '_id') AND NON_UNIQUE = 0 AND COLUMN_NAME = 'id'), CONCAT('SELECT ''', @table_name, ' already migrated'' AS migration_note'), CONCAT('ALTER TABLE ', @table_name, ' DROP PRIMARY KEY, ADD PRIMARY KEY (symbol, timestamp), ADD UNIQUE KEY uq_', @table_name, '_id (id), ALGORITHM=INPLACE, LOCK=NONE')));
PREPARE pk_stmt FROM @pk_sql; EXECUTE pk_stmt; DEALLOCATE PREPARE pk_stmt;

SET @table_name = 'klines_m15';
SET @pk_sql = IF(NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = @table_name AND TABLE_TYPE = 'BASE TABLE'), CONCAT('SELECT ''', @table_name, ' absent'' AS migration_note'), IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = @table_name AND CONSTRAINT_NAME = 'PRIMARY' AND COLUMN_NAME IN ('symbol', 'timestamp')) = 2 AND EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = @table_name AND INDEX_NAME = CONCAT('uq_', @table_name, '_id') AND NON_UNIQUE = 0 AND COLUMN_NAME = 'id'), CONCAT('SELECT ''', @table_name, ' already migrated'' AS migration_note'), CONCAT('ALTER TABLE ', @table_name, ' DROP PRIMARY KEY, ADD PRIMARY KEY (symbol, timestamp), ADD UNIQUE KEY uq_', @table_name, '_id (id), ALGORITHM=INPLACE, LOCK=NONE')));
PREPARE pk_stmt FROM @pk_sql; EXECUTE pk_stmt; DEALLOCATE PREPARE pk_stmt;

SET @table_name = 'klines_m30';
SET @pk_sql = IF(NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = @table_name AND TABLE_TYPE = 'BASE TABLE'), CONCAT('SELECT ''', @table_name, ' absent'' AS migration_note'), IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = @table_name AND CONSTRAINT_NAME = 'PRIMARY' AND COLUMN_NAME IN ('symbol', 'timestamp')) = 2 AND EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = @table_name AND INDEX_NAME = CONCAT('uq_', @table_name, '_id') AND NON_UNIQUE = 0 AND COLUMN_NAME = 'id'), CONCAT('SELECT ''', @table_name, ' already migrated'' AS migration_note'), CONCAT('ALTER TABLE ', @table_name, ' DROP PRIMARY KEY, ADD PRIMARY KEY (symbol, timestamp), ADD UNIQUE KEY uq_', @table_name, '_id (id), ALGORITHM=INPLACE, LOCK=NONE')));
PREPARE pk_stmt FROM @pk_sql; EXECUTE pk_stmt; DEALLOCATE PREPARE pk_stmt;

SET @table_name = 'klines_h1';
SET @pk_sql = IF(NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = @table_name AND TABLE_TYPE = 'BASE TABLE'), CONCAT('SELECT ''', @table_name, ' absent'' AS migration_note'), IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = @table_name AND CONSTRAINT_NAME = 'PRIMARY' AND COLUMN_NAME IN ('symbol', 'timestamp')) = 2 AND EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = @table_name AND INDEX_NAME = CONCAT('uq_', @table_name, '_id') AND NON_UNIQUE = 0 AND COLUMN_NAME = 'id'), CONCAT('SELECT ''', @table_name, ' already migrated'' AS migration_note'), CONCAT('ALTER TABLE ', @table_name, ' DROP PRIMARY KEY, ADD PRIMARY KEY (symbol, timestamp), ADD UNIQUE KEY uq_', @table_name, '_id (id), ALGORITHM=INPLACE, LOCK=NONE')));
PREPARE pk_stmt FROM @pk_sql; EXECUTE pk_stmt; DEALLOCATE PREPARE pk_stmt;

SET @table_name = 'klines_h2';
SET @pk_sql = IF(NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = @table_name AND TABLE_TYPE = 'BASE TABLE'), CONCAT('SELECT ''', @table_name, ' absent'' AS migration_note'), IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = @table_name AND CONSTRAINT_NAME = 'PRIMARY' AND COLUMN_NAME IN ('symbol', 'timestamp')) = 2 AND EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = @table_name AND INDEX_NAME = CONCAT('uq_', @table_name, '_id') AND NON_UNIQUE = 0 AND COLUMN_NAME = 'id'), CONCAT('SELECT ''', @table_name, ' already migrated'' AS migration_note'), CONCAT('ALTER TABLE ', @table_name, ' DROP PRIMARY KEY, ADD PRIMARY KEY (symbol, timestamp), ADD UNIQUE KEY uq_', @table_name, '_id (id), ALGORITHM=INPLACE, LOCK=NONE')));
PREPARE pk_stmt FROM @pk_sql; EXECUTE pk_stmt; DEALLOCATE PREPARE pk_stmt;

SET @table_name = 'klines_h4';
SET @pk_sql = IF(NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = @table_name AND TABLE_TYPE = 'BASE TABLE'), CONCAT('SELECT ''', @table_name, ' absent'' AS migration_note'), IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = @table_name AND CONSTRAINT_NAME = 'PRIMARY' AND COLUMN_NAME IN ('symbol', 'timestamp')) = 2 AND EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = @table_name AND INDEX_NAME = CONCAT('uq_', @table_name, '_id') AND NON_UNIQUE = 0 AND COLUMN_NAME = 'id'), CONCAT('SELECT ''', @table_name, ' already migrated'' AS migration_note'), CONCAT('ALTER TABLE ', @table_name, ' DROP PRIMARY KEY, ADD PRIMARY KEY (symbol, timestamp), ADD UNIQUE KEY uq_', @table_name, '_id (id), ALGORITHM=INPLACE, LOCK=NONE')));
PREPARE pk_stmt FROM @pk_sql; EXECUTE pk_stmt; DEALLOCATE PREPARE pk_stmt;

SET @table_name = 'klines_h6';
SET @pk_sql = IF(NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = @table_name AND TABLE_TYPE = 'BASE TABLE'), CONCAT('SELECT ''', @table_name, ' absent'' AS migration_note'), IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = @table_name AND CONSTRAINT_NAME = 'PRIMARY' AND COLUMN_NAME IN ('symbol', 'timestamp')) = 2 AND EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = @table_name AND INDEX_NAME = CONCAT('uq_', @table_name, '_id') AND NON_UNIQUE = 0 AND COLUMN_NAME = 'id'), CONCAT('SELECT ''', @table_name, ' already migrated'' AS migration_note'), CONCAT('ALTER TABLE ', @table_name, ' DROP PRIMARY KEY, ADD PRIMARY KEY (symbol, timestamp), ADD UNIQUE KEY uq_', @table_name, '_id (id), ALGORITHM=INPLACE, LOCK=NONE')));
PREPARE pk_stmt FROM @pk_sql; EXECUTE pk_stmt; DEALLOCATE PREPARE pk_stmt;

SET @table_name = 'klines_h8';
SET @pk_sql = IF(NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = @table_name AND TABLE_TYPE = 'BASE TABLE'), CONCAT('SELECT ''', @table_name, ' absent'' AS migration_note'), IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = @table_name AND CONSTRAINT_NAME = 'PRIMARY' AND COLUMN_NAME IN ('symbol', 'timestamp')) = 2 AND EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = @table_name AND INDEX_NAME = CONCAT('uq_', @table_name, '_id') AND NON_UNIQUE = 0 AND COLUMN_NAME = 'id'), CONCAT('SELECT ''', @table_name, ' already migrated'' AS migration_note'), CONCAT('ALTER TABLE ', @table_name, ' DROP PRIMARY KEY, ADD PRIMARY KEY (symbol, timestamp), ADD UNIQUE KEY uq_', @table_name, '_id (id), ALGORITHM=INPLACE, LOCK=NONE')));
PREPARE pk_stmt FROM @pk_sql; EXECUTE pk_stmt; DEALLOCATE PREPARE pk_stmt;

SET @table_name = 'klines_h12';
SET @pk_sql = IF(NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = @table_name AND TABLE_TYPE = 'BASE TABLE'), CONCAT('SELECT ''', @table_name, ' absent'' AS migration_note'), IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = @table_name AND CONSTRAINT_NAME = 'PRIMARY' AND COLUMN_NAME IN ('symbol', 'timestamp')) = 2 AND EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = @table_name AND INDEX_NAME = CONCAT('uq_', @table_name, '_id') AND NON_UNIQUE = 0 AND COLUMN_NAME = 'id'), CONCAT('SELECT ''', @table_name, ' already migrated'' AS migration_note'), CONCAT('ALTER TABLE ', @table_name, ' DROP PRIMARY KEY, ADD PRIMARY KEY (symbol, timestamp), ADD UNIQUE KEY uq_', @table_name, '_id (id), ALGORITHM=INPLACE, LOCK=NONE')));
PREPARE pk_stmt FROM @pk_sql; EXECUTE pk_stmt; DEALLOCATE PREPARE pk_stmt;

SET @table_name = 'klines_d1';
SET @pk_sql = IF(NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = @table_name AND TABLE_TYPE = 'BASE TABLE'), CONCAT('SELECT ''', @table_name, ' absent'' AS migration_note'), IF((SELECT COUNT(*) FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = @table_name AND CONSTRAINT_NAME = 'PRIMARY' AND COLUMN_NAME IN ('symbol', 'timestamp')) = 2 AND EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = @table_name AND INDEX_NAME = CONCAT('uq_', @table_name, '_id') AND NON_UNIQUE = 0 AND COLUMN_NAME = 'id'), CONCAT('SELECT ''', @table_name, ' already migrated'' AS migration_note'), CONCAT('ALTER TABLE ', @table_name, ' DROP PRIMARY KEY, ADD PRIMARY KEY (symbol, timestamp), ADD UNIQUE KEY uq_', @table_name, '_id (id), ALGORITHM=INPLACE, LOCK=NONE')));
PREPARE pk_stmt FROM @pk_sql; EXECUTE pk_stmt; DEALLOCATE PREPARE pk_stmt;
