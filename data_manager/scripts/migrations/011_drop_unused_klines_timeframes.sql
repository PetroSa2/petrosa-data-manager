-- Migration 011 — retire the seven never-populated MySQL klines tables.
-- Ticket: PetroSa2/petrosa_k8s#1173 (EPIC-1158, filed separately from W7/#1164).
--
-- WHY THESE SEVEN ARE SAFE TO DROP
--
-- Verified read-only against production on 2026-09-29:
--
--   * All seven have COUNT(*) = 0 and have held 0 rows since creation.
--     INFORMATION_SCHEMA.CREATE_TIME is 2026-03-13 08:19:0X for all seven —
--     one schema DDL run — versus 2026-09-23 08:1X for the five tables that
--     are actually written (klines_d1, klines_h1, klines_m5, klines_m15,
--     klines_m30). DATA_FREE is 0 on six of the seven, confirming nothing
--     was ever deleted either; klines_m1 retains 76 MB of DATA_FREE from a
--     pre-March fill that is reclaimed simply by dropping the table.
--   * No writer exists. petrosa_k8s/k8s/data-extractor/klines-extractor-cronjobs.yaml
--     and klines-gap-filler-cronjob.yaml create CronJobs for exactly
--     5m, 15m, 30m, 1h, d1. There is no 1m, 3m, 2h, 4h, 6h, 8h or 12h CronJob.
--   * No execution-path reader exists. constants.SUPPORTED_INTERVALS
--     (petrosa-common-config: "5m,15m,30m,1h,1d") excludes 1m and 4h.
--   * No candle-store reader exists. constants.CANDLE_DATABASE_TYPE defaults
--     to "mongodb" and is set in ZERO production deployments, so every
--     MySQL branch in CandleRepository (_primary_is_mysql()) is unreachable.
--     CANDLE_DUAL_WRITE_ENABLED is unset (false), so the MySQL candle mirror
--     receives no writes either.
--   * The public read surface is closed in the same change:
--     GET /api/v1/data/candles now validates `period` against
--     constants.SUPPORTED_TIMEFRAMES and returns 422 otherwise, and
--     SUPPORTED_TIMEFRAMES no longer contains 1m or 4h.
--
-- SEVEN VIEWS MUST BE DROPPED FIRST
--
-- INFORMATION_SCHEMA.TABLES reports 24 objects matching 'klines_%', but only 12
-- are BASE TABLEs. The other 12 are compatibility VIEWS that map the
-- long-form Binance name onto the short-form base table:
--
--   view        -> base table                 verdict
--   klines_1m   -> klines_m1                  drop (base is dead)
--   klines_3m   -> klines_m3                  drop (base is dead)
--   klines_2h   -> klines_h2                  drop (base is dead)
--   klines_4h   -> klines_h4                  drop (base is dead)
--   klines_6h   -> klines_h6                  drop (base is dead)
--   klines_8h   -> klines_h8                  drop (base is dead)
--   klines_12h  -> klines_h12                 drop (base is dead)
--   klines_5m   -> klines_m5                  KEEP (base is live)
--   klines_15m  -> klines_m15                 KEEP (base is live)
--   klines_30m  -> klines_m30                 KEEP (base is live)
--   klines_1h   -> klines_h1                  KEEP (base is live)
--   klines_1d   -> klines_d1                  KEEP (base is live)
--
-- Dropping a base table without dropping its views first leaves seven
-- permanently-broken views that still resolve by name and fail at query time
-- with "Table 'db.klines_m1' doesn't exist" — turning an absent dataset into a
-- runtime error on every call. DROP VIEW is issued first for exactly this
-- reason. Any other views in the schema (contribution_summary,
-- strategy_performance) are unrelated and must not be touched.
--
-- CONSEQUENCE FOR THE MySQL CANDLE ROLLBACK PATH
--
-- Reverting CANDLE_DATABASE_TYPE to "mysql" is no longer a supported
-- operation for 1m/4h: those timeframes are not in SUPPORTED_TIMEFRAMES, so
-- the API rejects them before reaching the adapter. Re-adding a timeframe
-- here without also adding its extractor CronJob would restore the silent
-- empty-table condition this migration removes.
--
-- MySQL 5.x compatible and idempotent: each table is guarded through
-- INFORMATION_SCHEMA before the DROP is prepared.

SELECT COUNT(*) INTO @t_m1 FROM INFORMATION_SCHEMA.TABLES
 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m1' AND TABLE_TYPE = 'BASE TABLE';
SELECT COUNT(*) INTO @t_m3 FROM INFORMATION_SCHEMA.TABLES
 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m3' AND TABLE_TYPE = 'BASE TABLE';
SELECT COUNT(*) INTO @t_h2 FROM INFORMATION_SCHEMA.TABLES
 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_h2' AND TABLE_TYPE = 'BASE TABLE';
SELECT COUNT(*) INTO @t_h4 FROM INFORMATION_SCHEMA.TABLES
 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_h4' AND TABLE_TYPE = 'BASE TABLE';
SELECT COUNT(*) INTO @t_h6 FROM INFORMATION_SCHEMA.TABLES
 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_h6' AND TABLE_TYPE = 'BASE TABLE';
SELECT COUNT(*) INTO @t_h8 FROM INFORMATION_SCHEMA.TABLES
 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_h8' AND TABLE_TYPE = 'BASE TABLE';
SELECT COUNT(*) INTO @t_h12 FROM INFORMATION_SCHEMA.TABLES
 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_h12' AND TABLE_TYPE = 'BASE TABLE';

-- Report non-empty tables loudly. If any value below is non-zero, STOP and
-- investigate before applying the DROP: a populated table means the
-- "never written" premise is wrong for that timeframe.
SELECT 'klines_m1' t, @t_m1 present UNION ALL SELECT 'klines_m3', @t_m3
UNION ALL SELECT 'klines_h2', @t_h2 UNION ALL SELECT 'klines_h4', @t_h4
UNION ALL SELECT 'klines_h6', @t_h6 UNION ALL SELECT 'klines_h8', @t_h8
UNION ALL SELECT 'klines_h12', @t_h12
UNION ALL SELECT 'klines_m5 (MUST REMAIN)', COUNT(*) FROM INFORMATION_SCHEMA.TABLES
 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m5'
UNION ALL SELECT 'klines_m15 (MUST REMAIN)', COUNT(*) FROM INFORMATION_SCHEMA.TABLES
 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m15'
UNION ALL SELECT 'klines_m30 (MUST REMAIN)', COUNT(*) FROM INFORMATION_SCHEMA.TABLES
 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m30'
UNION ALL SELECT 'klines_h1 (MUST REMAIN)', COUNT(*) FROM INFORMATION_SCHEMA.TABLES
 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_h1'
UNION ALL SELECT 'klines_d1 (MUST REMAIN)', COUNT(*) FROM INFORMATION_SCHEMA.TABLES
 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_d1';

SELECT COUNT(*) INTO @v_m1 FROM INFORMATION_SCHEMA.VIEWS
 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_1m';
SELECT COUNT(*) INTO @v_m3 FROM INFORMATION_SCHEMA.VIEWS
 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_3m';
SELECT COUNT(*) INTO @v_h2 FROM INFORMATION_SCHEMA.VIEWS
 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_2h';
SELECT COUNT(*) INTO @v_h4 FROM INFORMATION_SCHEMA.VIEWS
 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_4h';
SELECT COUNT(*) INTO @v_h6 FROM INFORMATION_SCHEMA.VIEWS
 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_6h';
SELECT COUNT(*) INTO @v_h8 FROM INFORMATION_SCHEMA.VIEWS
 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_8h';
SELECT COUNT(*) INTO @v_h12 FROM INFORMATION_SCHEMA.VIEWS
 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_12h';

SET @drop_view_sql = IF(
    @v_m1 + @v_m3 + @v_h2 + @v_h4 + @v_h6 + @v_h8 + @v_h12 > 0,
    'DROP VIEW IF EXISTS klines_1m, klines_3m, klines_2h, klines_4h, klines_6h, klines_8h, klines_12h',
    'SELECT ''all seven retired klines views already absent; view drop skipped'' AS migration_note'
);
PREPARE drop_klines_view_stmt FROM @drop_view_sql;
EXECUTE drop_klines_view_stmt;
DEALLOCATE PREPARE drop_klines_view_stmt;

-- The five live compatibility views must survive. A non-zero value here after
-- the DROP VIEW above means this migration touched a view it must not have.
SELECT 'klines_5m (KEEP)' v, COUNT(*) FROM INFORMATION_SCHEMA.VIEWS
 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_5m'
UNION ALL SELECT 'klines_15m (KEEP)', COUNT(*) FROM INFORMATION_SCHEMA.VIEWS
 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_15m'
UNION ALL SELECT 'klines_30m (KEEP)', COUNT(*) FROM INFORMATION_SCHEMA.VIEWS
 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_30m'
UNION ALL SELECT 'klines_1h (KEEP)', COUNT(*) FROM INFORMATION_SCHEMA.VIEWS
 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_1h'
UNION ALL SELECT 'klines_1d (KEEP)', COUNT(*) FROM INFORMATION_SCHEMA.VIEWS
 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_1d';

SET @drop_sql = IF(
    @t_m1 + @t_m3 + @t_h2 + @t_h4 + @t_h6 + @t_h8 + @t_h12 > 0,
    'DROP TABLE IF EXISTS klines_m1, klines_m3, klines_h2, klines_h4, klines_h6, klines_h8, klines_h12',
    'SELECT ''all seven retired klines tables already absent; drop skipped'' AS migration_note'
);
PREPARE drop_klines_stmt FROM @drop_sql;
EXECUTE drop_klines_stmt;
DEALLOCATE PREPARE drop_klines_stmt;

-- Post-conditions: the five written tables must all still be present.
SELECT 'klines_m5', COUNT(*) FROM INFORMATION_SCHEMA.TABLES
 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m5'
UNION ALL SELECT 'klines_m15', COUNT(*) FROM INFORMATION_SCHEMA.TABLES
 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m15'
UNION ALL SELECT 'klines_m30', COUNT(*) FROM INFORMATION_SCHEMA.TABLES
 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m30'
UNION ALL SELECT 'klines_h1', COUNT(*) FROM INFORMATION_SCHEMA.TABLES
 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_h1'
UNION ALL SELECT 'klines_d1', COUNT(*) FROM INFORMATION_SCHEMA.TABLES
 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_d1';
