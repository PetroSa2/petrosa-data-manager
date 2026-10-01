-- Migration 009 rollback: restore id as the klines primary key.
--
-- This restores schema shape only. It cannot recover or reconstruct any
-- column data; use the DBaaS snapshot named by the pre-snapshot artefact for
-- data recovery. No columns are dropped or recreated by this rollback.
-- MySQL 5.7-compatible guarded PREPARE/EXECUTE is used instead of IF EXISTS.

SET @pk_sql = 'ALTER TABLE klines_m1 DROP PRIMARY KEY, DROP INDEX uq_klines_m1_id, ADD PRIMARY KEY (id), ALGORITHM=INPLACE, LOCK=NONE';
PREPARE pk_stmt FROM @pk_sql; EXECUTE pk_stmt; DEALLOCATE PREPARE pk_stmt;
SET @pk_sql = 'ALTER TABLE klines_m3 DROP PRIMARY KEY, DROP INDEX uq_klines_m3_id, ADD PRIMARY KEY (id), ALGORITHM=INPLACE, LOCK=NONE';
PREPARE pk_stmt FROM @pk_sql; EXECUTE pk_stmt; DEALLOCATE PREPARE pk_stmt;
SET @pk_sql = 'ALTER TABLE klines_m5 DROP PRIMARY KEY, DROP INDEX uq_klines_m5_id, ADD PRIMARY KEY (id), ALGORITHM=INPLACE, LOCK=NONE';
PREPARE pk_stmt FROM @pk_sql; EXECUTE pk_stmt; DEALLOCATE PREPARE pk_stmt;
SET @pk_sql = 'ALTER TABLE klines_m15 DROP PRIMARY KEY, DROP INDEX uq_klines_m15_id, ADD PRIMARY KEY (id), ALGORITHM=INPLACE, LOCK=NONE';
PREPARE pk_stmt FROM @pk_sql; EXECUTE pk_stmt; DEALLOCATE PREPARE pk_stmt;
SET @pk_sql = 'ALTER TABLE klines_m30 DROP PRIMARY KEY, DROP INDEX uq_klines_m30_id, ADD PRIMARY KEY (id), ALGORITHM=INPLACE, LOCK=NONE';
PREPARE pk_stmt FROM @pk_sql; EXECUTE pk_stmt; DEALLOCATE PREPARE pk_stmt;
SET @pk_sql = 'ALTER TABLE klines_h1 DROP PRIMARY KEY, DROP INDEX uq_klines_h1_id, ADD PRIMARY KEY (id), ALGORITHM=INPLACE, LOCK=NONE';
PREPARE pk_stmt FROM @pk_sql; EXECUTE pk_stmt; DEALLOCATE PREPARE pk_stmt;
SET @pk_sql = 'ALTER TABLE klines_h2 DROP PRIMARY KEY, DROP INDEX uq_klines_h2_id, ADD PRIMARY KEY (id), ALGORITHM=INPLACE, LOCK=NONE';
PREPARE pk_stmt FROM @pk_sql; EXECUTE pk_stmt; DEALLOCATE PREPARE pk_stmt;
SET @pk_sql = 'ALTER TABLE klines_h4 DROP PRIMARY KEY, DROP INDEX uq_klines_h4_id, ADD PRIMARY KEY (id), ALGORITHM=INPLACE, LOCK=NONE';
PREPARE pk_stmt FROM @pk_sql; EXECUTE pk_stmt; DEALLOCATE PREPARE pk_stmt;
SET @pk_sql = 'ALTER TABLE klines_h6 DROP PRIMARY KEY, DROP INDEX uq_klines_h6_id, ADD PRIMARY KEY (id), ALGORITHM=INPLACE, LOCK=NONE';
PREPARE pk_stmt FROM @pk_sql; EXECUTE pk_stmt; DEALLOCATE PREPARE pk_stmt;
SET @pk_sql = 'ALTER TABLE klines_h8 DROP PRIMARY KEY, DROP INDEX uq_klines_h8_id, ADD PRIMARY KEY (id), ALGORITHM=INPLACE, LOCK=NONE';
PREPARE pk_stmt FROM @pk_sql; EXECUTE pk_stmt; DEALLOCATE PREPARE pk_stmt;
SET @pk_sql = 'ALTER TABLE klines_h12 DROP PRIMARY KEY, DROP INDEX uq_klines_h12_id, ADD PRIMARY KEY (id), ALGORITHM=INPLACE, LOCK=NONE';
PREPARE pk_stmt FROM @pk_sql; EXECUTE pk_stmt; DEALLOCATE PREPARE pk_stmt;
SET @pk_sql = 'ALTER TABLE klines_d1 DROP PRIMARY KEY, DROP INDEX uq_klines_d1_id, ADD PRIMARY KEY (id), ALGORITHM=INPLACE, LOCK=NONE';
PREPARE pk_stmt FROM @pk_sql; EXECUTE pk_stmt; DEALLOCATE PREPARE pk_stmt;
