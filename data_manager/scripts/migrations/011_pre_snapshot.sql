-- Migration 011 pre-change snapshot query — PetroSa2/petrosa_k8s#1173.
--
-- Run against the target database immediately before applying
-- 011_drop_unused_klines_timeframes.sql, then replace the committed
-- PENDING-OPERATOR fixture below with the captured output.
--
-- The load-bearing assertion is that every one of the seven retirements reads
-- ROWS = 0. 011 itself only checks that the table *exists*; it cannot check
-- emptiness cheaply, because COUNT(*) on a table you are about to drop is the
-- one thing worth confirming. A non-zero ROWS value here means the "never
-- written" premise is wrong for that timeframe — STOP and investigate rather
-- than applying the DROP.
--
-- 007_pre_snapshot.sql also dumps SHOW CREATE TABLE for klines_m1 and klines_h4;
-- 011 does not repeat that because 011_rollback already carries the verbatim
-- captured DDL, and having two copies of the same schema to keep in sync is
-- how they drift.

SELECT TABLE_NAME, TABLE_ROWS, DATA_LENGTH, INDEX_LENGTH, DATA_FREE,
       CREATE_TIME, TABLE_COLLATION
  FROM INFORMATION_SCHEMA.TABLES
 WHERE TABLE_SCHEMA = DATABASE()
   AND TABLE_NAME IN (
     'klines_m1', 'klines_m3', 'klines_h2', 'klines_h4',
     'klines_h6', 'klines_h8', 'klines_h12'
   )
 ORDER BY TABLE_NAME;

-- The five tables that MUST survive. Capture these too: they are the
-- regression baseline for verifying 011 dropped only what it intended to.
SELECT TABLE_NAME, TABLE_ROWS, DATA_LENGTH, INDEX_LENGTH, DATA_FREE
  FROM INFORMATION_SCHEMA.TABLES
 WHERE TABLE_SCHEMA = DATABASE()
   AND TABLE_NAME IN ('klines_m5', 'klines_m15', 'klines_m30', 'klines_h1', 'klines_d1')
 ORDER BY TABLE_NAME;

-- Exact emptiness proof, ordered smallest-first so the first failure is cheap
-- to see. Every row must read 0.
SELECT 'klines_h4' AS t, COUNT(*) AS rows_present FROM klines_h4
UNION ALL SELECT 'klines_h2', COUNT(*) FROM klines_h2
UNION ALL SELECT 'klines_h6', COUNT(*) FROM klines_h6
UNION ALL SELECT 'klines_h8', COUNT(*) FROM klines_h8
UNION ALL SELECT 'klines_h12', COUNT(*) FROM klines_h12
UNION ALL SELECT 'klines_m3', COUNT(*) FROM klines_m3
UNION ALL SELECT 'klines_m1', COUNT(*) FROM klines_m1;
