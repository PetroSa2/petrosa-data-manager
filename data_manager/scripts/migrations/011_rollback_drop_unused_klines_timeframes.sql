-- Rollback for migration 011 (PetroSa2/petrosa_k8s#1173).
--
-- The seven dropped tables were empty when dropped, so recreating them
-- restores the prior schema exactly — no data is recovered because none
-- existed. This is the inverse of a DROP and is safe to run at any time.
--
-- The DDL below is the verbatim `SHOW CREATE TABLE` captured from production on
-- 2026-09-29, not a hand-written approximation. It is the pre-W4 shape:
-- `id varchar(64)` primary key, three secondary KEYs, 18 wide decimal columns.
-- W4 (#1163) plans to replace this with PRIMARY KEY (symbol, timestamp) and
-- drop the dead columns. Do not "improve" this DDL to the post-W4 shape — the
-- rollback would reintroduce a schema the rest of the ecosystem has moved past.
--
-- Each table is spelled out in full rather than reusing CREATE TABLE ... LIKE.
-- LIKE copies the source table's index *names*, so klines_h4 would come back
-- carrying idx_klines_m1_* — wrong, and confusing when reading SHOW CREATE
-- TABLE during a recovery.
--
-- If a retired timeframe is ever wanted back, the correct order is:
--   1. Add its extractor + gap-filler CronJobs in petrosa_k8s so the table
--      actually receives rows.
--   2. Re-add the timeframe to constants.SUPPORTED_TIMEFRAMES.
--   3. Recreate the table (this script) so the writer does not error.
-- Doing (3) without (1) recreates the silent empty-table condition that
-- migration 011 exists to remove.
--
-- MySQL 5.x compatible and idempotent.

CREATE TABLE IF NOT EXISTS `klines_m1` (
  `id` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `symbol` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `timestamp` datetime NOT NULL,
  `open_time` datetime NOT NULL,
  `close_time` datetime NOT NULL,
  `interval` varchar(10) COLLATE utf8mb4_unicode_ci NOT NULL,
  `open_price` decimal(20,8) NOT NULL,
  `high_price` decimal(20,8) NOT NULL,
  `low_price` decimal(20,8) NOT NULL,
  `close_price` decimal(20,8) NOT NULL,
  `volume` decimal(20,8) NOT NULL,
  `quote_asset_volume` decimal(20,8) NOT NULL,
  `number_of_trades` int(11) NOT NULL,
  `taker_buy_base_asset_volume` decimal(20,8) NOT NULL,
  `taker_buy_quote_asset_volume` decimal(20,8) NOT NULL,
  `price_change` decimal(20,8) DEFAULT NULL,
  `price_change_percent` decimal(10,4) DEFAULT NULL,
  `extracted_at` datetime NOT NULL,
  `extractor_version` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `source` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_klines_m1_symbol_timestamp` (`symbol`,`timestamp`),
  KEY `idx_klines_m1_timestamp` (`timestamp`),
  KEY `idx_klines_m1_open_time` (`open_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `klines_m3` (
  `id` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `symbol` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `timestamp` datetime NOT NULL,
  `open_time` datetime NOT NULL,
  `close_time` datetime NOT NULL,
  `interval` varchar(10) COLLATE utf8mb4_unicode_ci NOT NULL,
  `open_price` decimal(20,8) NOT NULL,
  `high_price` decimal(20,8) NOT NULL,
  `low_price` decimal(20,8) NOT NULL,
  `close_price` decimal(20,8) NOT NULL,
  `volume` decimal(20,8) NOT NULL,
  `quote_asset_volume` decimal(20,8) NOT NULL,
  `number_of_trades` int(11) NOT NULL,
  `taker_buy_base_asset_volume` decimal(20,8) NOT NULL,
  `taker_buy_quote_asset_volume` decimal(20,8) NOT NULL,
  `price_change` decimal(20,8) DEFAULT NULL,
  `price_change_percent` decimal(10,4) DEFAULT NULL,
  `extracted_at` datetime NOT NULL,
  `extractor_version` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `source` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_klines_m3_symbol_timestamp` (`symbol`,`timestamp`),
  KEY `idx_klines_m3_timestamp` (`timestamp`),
  KEY `idx_klines_m3_open_time` (`open_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `klines_h2` (
  `id` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `symbol` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `timestamp` datetime NOT NULL,
  `open_time` datetime NOT NULL,
  `close_time` datetime NOT NULL,
  `interval` varchar(10) COLLATE utf8mb4_unicode_ci NOT NULL,
  `open_price` decimal(20,8) NOT NULL,
  `high_price` decimal(20,8) NOT NULL,
  `low_price` decimal(20,8) NOT NULL,
  `close_price` decimal(20,8) NOT NULL,
  `volume` decimal(20,8) NOT NULL,
  `quote_asset_volume` decimal(20,8) NOT NULL,
  `number_of_trades` int(11) NOT NULL,
  `taker_buy_base_asset_volume` decimal(20,8) NOT NULL,
  `taker_buy_quote_asset_volume` decimal(20,8) NOT NULL,
  `price_change` decimal(20,8) DEFAULT NULL,
  `price_change_percent` decimal(10,4) DEFAULT NULL,
  `extracted_at` datetime NOT NULL,
  `extractor_version` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `source` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_klines_h2_symbol_timestamp` (`symbol`,`timestamp`),
  KEY `idx_klines_h2_timestamp` (`timestamp`),
  KEY `idx_klines_h2_open_time` (`open_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `klines_h4` (
  `id` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `symbol` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `timestamp` datetime NOT NULL,
  `open_time` datetime NOT NULL,
  `close_time` datetime NOT NULL,
  `interval` varchar(10) COLLATE utf8mb4_unicode_ci NOT NULL,
  `open_price` decimal(20,8) NOT NULL,
  `high_price` decimal(20,8) NOT NULL,
  `low_price` decimal(20,8) NOT NULL,
  `close_price` decimal(20,8) NOT NULL,
  `volume` decimal(20,8) NOT NULL,
  `quote_asset_volume` decimal(20,8) NOT NULL,
  `number_of_trades` int(11) NOT NULL,
  `taker_buy_base_asset_volume` decimal(20,8) NOT NULL,
  `taker_buy_quote_asset_volume` decimal(20,8) NOT NULL,
  `price_change` decimal(20,8) DEFAULT NULL,
  `price_change_percent` decimal(10,4) DEFAULT NULL,
  `extracted_at` datetime NOT NULL,
  `extractor_version` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `source` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_klines_h4_symbol_timestamp` (`symbol`,`timestamp`),
  KEY `idx_klines_h4_timestamp` (`timestamp`),
  KEY `idx_klines_h4_open_time` (`open_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `klines_h6` (
  `id` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `symbol` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `timestamp` datetime NOT NULL,
  `open_time` datetime NOT NULL,
  `close_time` datetime NOT NULL,
  `interval` varchar(10) COLLATE utf8mb4_unicode_ci NOT NULL,
  `open_price` decimal(20,8) NOT NULL,
  `high_price` decimal(20,8) NOT NULL,
  `low_price` decimal(20,8) NOT NULL,
  `close_price` decimal(20,8) NOT NULL,
  `volume` decimal(20,8) NOT NULL,
  `quote_asset_volume` decimal(20,8) NOT NULL,
  `number_of_trades` int(11) NOT NULL,
  `taker_buy_base_asset_volume` decimal(20,8) NOT NULL,
  `taker_buy_quote_asset_volume` decimal(20,8) NOT NULL,
  `price_change` decimal(20,8) DEFAULT NULL,
  `price_change_percent` decimal(10,4) DEFAULT NULL,
  `extracted_at` datetime NOT NULL,
  `extractor_version` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `source` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_klines_h6_symbol_timestamp` (`symbol`,`timestamp`),
  KEY `idx_klines_h6_timestamp` (`timestamp`),
  KEY `idx_klines_h6_open_time` (`open_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `klines_h8` (
  `id` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `symbol` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `timestamp` datetime NOT NULL,
  `open_time` datetime NOT NULL,
  `close_time` datetime NOT NULL,
  `interval` varchar(10) COLLATE utf8mb4_unicode_ci NOT NULL,
  `open_price` decimal(20,8) NOT NULL,
  `high_price` decimal(20,8) NOT NULL,
  `low_price` decimal(20,8) NOT NULL,
  `close_price` decimal(20,8) NOT NULL,
  `volume` decimal(20,8) NOT NULL,
  `quote_asset_volume` decimal(20,8) NOT NULL,
  `number_of_trades` int(11) NOT NULL,
  `taker_buy_base_asset_volume` decimal(20,8) NOT NULL,
  `taker_buy_quote_asset_volume` decimal(20,8) NOT NULL,
  `price_change` decimal(20,8) DEFAULT NULL,
  `price_change_percent` decimal(10,4) DEFAULT NULL,
  `extracted_at` datetime NOT NULL,
  `extractor_version` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `source` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_klines_h8_symbol_timestamp` (`symbol`,`timestamp`),
  KEY `idx_klines_h8_timestamp` (`timestamp`),
  KEY `idx_klines_h8_open_time` (`open_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `klines_h12` (
  `id` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `symbol` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `timestamp` datetime NOT NULL,
  `open_time` datetime NOT NULL,
  `close_time` datetime NOT NULL,
  `interval` varchar(10) COLLATE utf8mb4_unicode_ci NOT NULL,
  `open_price` decimal(20,8) NOT NULL,
  `high_price` decimal(20,8) NOT NULL,
  `low_price` decimal(20,8) NOT NULL,
  `close_price` decimal(20,8) NOT NULL,
  `volume` decimal(20,8) NOT NULL,
  `quote_asset_volume` decimal(20,8) NOT NULL,
  `number_of_trades` int(11) NOT NULL,
  `taker_buy_base_asset_volume` decimal(20,8) NOT NULL,
  `taker_buy_quote_asset_volume` decimal(20,8) NOT NULL,
  `price_change` decimal(20,8) DEFAULT NULL,
  `price_change_percent` decimal(10,4) DEFAULT NULL,
  `extracted_at` datetime NOT NULL,
  `extractor_version` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `source` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_klines_h12_symbol_timestamp` (`symbol`,`timestamp`),
  KEY `idx_klines_h12_timestamp` (`timestamp`),
  KEY `idx_klines_h12_open_time` (`open_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- klines_m1 additionally carried uniq_klines_m1_symbol_timestamp from
-- migration 008. Its primary key is `id`, so that UNIQUE index is NOT a
-- duplicate of the PK here. Recreate it only if 008 had been applied before
-- the table was dropped; the guard makes re-running 008 an equivalent path.
SELECT COUNT(*) INTO @uniq_m1_exists FROM INFORMATION_SCHEMA.STATISTICS
 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'klines_m1'
   AND INDEX_NAME = 'uniq_klines_m1_symbol_timestamp';

SET @uniq_sql = IF(
    @uniq_m1_exists = 0,
    'ALTER TABLE klines_m1 ADD UNIQUE INDEX uniq_klines_m1_symbol_timestamp (symbol, timestamp)',
    'SELECT ''uniq_klines_m1_symbol_timestamp already present'' AS migration_note'
);
PREPARE uniq_m1_stmt FROM @uniq_sql;
EXECUTE uniq_m1_stmt;
DEALLOCATE PREPARE uniq_m1_stmt;

-- Recreate the seven compatibility views the forward migration dropped.
--
-- This is the half of the rollback that is easy to forget and expensive to get
-- wrong: without it, a rollback restores the base tables but leaves
-- klines_1m / klines_3m / klines_2h / klines_4h / klines_6h / klines_8h /
-- klines_12h absent, so any caller still using a long-form timeframe name
-- fails with "table doesn't exist" against an otherwise complete database.
-- A rollback that does not restore the shape it removed is not a rollback.
--
-- DEFINER and SQL SECURITY are reproduced from the production
-- SHOW CREATE VIEW output captured 2026-09-09/29; dropping and recreating a
-- view would otherwise reset them to the executing user.
--
-- These recreate as views rather than as the base tables they shadow, matching
-- production: the long-form name is always a view over the short-form table.
CREATE OR REPLACE ALGORITHM=UNDEFINED
  DEFINER=`petrosa_crypto`@`%` SQL SECURITY DEFINER VIEW `klines_1m` AS
  select `klines_m1`.`id` AS `id`,`klines_m1`.`symbol` AS `symbol`,
    `klines_m1`.`timestamp` AS `timestamp`,`klines_m1`.`open_time` AS `open_time`,
    `klines_m1`.`close_time` AS `close_time`,`klines_m1`.`interval` AS `interval`,
    `klines_m1`.`open_price` AS `open_price`,`klines_m1`.`high_price` AS `high_price`,
    `klines_m1`.`low_price` AS `low_price`,`klines_m1`.`close_price` AS `close_price`,
    `klines_m1`.`volume` AS `volume`,`klines_m1`.`quote_asset_volume` AS `quote_asset_volume`,
    `klines_m1`.`number_of_trades` AS `number_of_trades`,
    `klines_m1`.`taker_buy_base_asset_volume` AS `taker_buy_base_asset_volume`,
    `klines_m1`.`taker_buy_quote_asset_volume` AS `taker_buy_quote_asset_volume`,
    `klines_m1`.`price_change` AS `price_change`,
    `klines_m1`.`price_change_percent` AS `price_change_percent`,
    `klines_m1`.`extracted_at` AS `extracted_at`,
    `klines_m1`.`extractor_version` AS `extractor_version`,
    `klines_m1`.`source` AS `source` from `klines_m1`;

CREATE OR REPLACE ALGORITHM=UNDEFINED
  DEFINER=`petrosa_crypto`@`%` SQL SECURITY DEFINER VIEW `klines_3m` AS
  select `klines_m3`.`id` AS `id`,`klines_m3`.`symbol` AS `symbol`,
    `klines_m3`.`timestamp` AS `timestamp`,`klines_m3`.`open_time` AS `open_time`,
    `klines_m3`.`close_time` AS `close_time`,`klines_m3`.`interval` AS `interval`,
    `klines_m3`.`open_price` AS `open_price`,`klines_m3`.`high_price` AS `high_price`,
    `klines_m3`.`low_price` AS `low_price`,`klines_m3`.`close_price` AS `close_price`,
    `klines_m3`.`volume` AS `volume`,`klines_m3`.`quote_asset_volume` AS `quote_asset_volume`,
    `klines_m3`.`number_of_trades` AS `number_of_trades`,
    `klines_m3`.`taker_buy_base_asset_volume` AS `taker_buy_base_asset_volume`,
    `klines_m3`.`taker_buy_quote_asset_volume` AS `taker_buy_quote_asset_volume`,
    `klines_m3`.`price_change` AS `price_change`,
    `klines_m3`.`price_change_percent` AS `price_change_percent`,
    `klines_m3`.`extracted_at` AS `extracted_at`,
    `klines_m3`.`extractor_version` AS `extractor_version`,
    `klines_m3`.`source` AS `source` from `klines_m3`;

CREATE OR REPLACE ALGORITHM=UNDEFINED
  DEFINER=`petrosa_crypto`@`%` SQL SECURITY DEFINER VIEW `klines_2h` AS
  select `klines_h2`.`id` AS `id`,`klines_h2`.`symbol` AS `symbol`,
    `klines_h2`.`timestamp` AS `timestamp`,`klines_h2`.`open_time` AS `open_time`,
    `klines_h2`.`close_time` AS `close_time`,`klines_h2`.`interval` AS `interval`,
    `klines_h2`.`open_price` AS `open_price`,`klines_h2`.`high_price` AS `high_price`,
    `klines_h2`.`low_price` AS `low_price`,`klines_h2`.`close_price` AS `close_price`,
    `klines_h2`.`volume` AS `volume`,`klines_h2`.`quote_asset_volume` AS `quote_asset_volume`,
    `klines_h2`.`number_of_trades` AS `number_of_trades`,
    `klines_h2`.`taker_buy_base_asset_volume` AS `taker_buy_base_asset_volume`,
    `klines_h2`.`taker_buy_quote_asset_volume` AS `taker_buy_quote_asset_volume`,
    `klines_h2`.`price_change` AS `price_change`,
    `klines_h2`.`price_change_percent` AS `price_change_percent`,
    `klines_h2`.`extracted_at` AS `extracted_at`,
    `klines_h2`.`extractor_version` AS `extractor_version`,
    `klines_h2`.`source` AS `source` from `klines_h2`;

CREATE OR REPLACE ALGORITHM=UNDEFINED
  DEFINER=`petrosa_crypto`@`%` SQL SECURITY DEFINER VIEW `klines_4h` AS
  select `klines_h4`.`id` AS `id`,`klines_h4`.`symbol` AS `symbol`,
    `klines_h4`.`timestamp` AS `timestamp`,`klines_h4`.`open_time` AS `open_time`,
    `klines_h4`.`close_time` AS `close_time`,`klines_h4`.`interval` AS `interval`,
    `klines_h4`.`open_price` AS `open_price`,`klines_h4`.`high_price` AS `high_price`,
    `klines_h4`.`low_price` AS `low_price`,`klines_h4`.`close_price` AS `close_price`,
    `klines_h4`.`volume` AS `volume`,`klines_h4`.`quote_asset_volume` AS `quote_asset_volume`,
    `klines_h4`.`number_of_trades` AS `number_of_trades`,
    `klines_h4`.`taker_buy_base_asset_volume` AS `taker_buy_base_asset_volume`,
    `klines_h4`.`taker_buy_quote_asset_volume` AS `taker_buy_quote_asset_volume`,
    `klines_h4`.`price_change` AS `price_change`,
    `klines_h4`.`price_change_percent` AS `price_change_percent`,
    `klines_h4`.`extracted_at` AS `extracted_at`,
    `klines_h4`.`extractor_version` AS `extractor_version`,
    `klines_h4`.`source` AS `source` from `klines_h4`;

CREATE OR REPLACE ALGORITHM=UNDEFINED
  DEFINER=`petrosa_crypto`@`%` SQL SECURITY DEFINER VIEW `klines_6h` AS
  select `klines_h6`.`id` AS `id`,`klines_h6`.`symbol` AS `symbol`,
    `klines_h6`.`timestamp` AS `timestamp`,`klines_h6`.`open_time` AS `open_time`,
    `klines_h6`.`close_time` AS `close_time`,`klines_h6`.`interval` AS `interval`,
    `klines_h6`.`open_price` AS `open_price`,`klines_h6`.`high_price` AS `high_price`,
    `klines_h6`.`low_price` AS `low_price`,`klines_h6`.`close_price` AS `close_price`,
    `klines_h6`.`volume` AS `volume`,`klines_h6`.`quote_asset_volume` AS `quote_asset_volume`,
    `klines_h6`.`number_of_trades` AS `number_of_trades`,
    `klines_h6`.`taker_buy_base_asset_volume` AS `taker_buy_base_asset_volume`,
    `klines_h6`.`taker_buy_quote_asset_volume` AS `taker_buy_quote_asset_volume`,
    `klines_h6`.`price_change` AS `price_change`,
    `klines_h6`.`price_change_percent` AS `price_change_percent`,
    `klines_h6`.`extracted_at` AS `extracted_at`,
    `klines_h6`.`extractor_version` AS `extractor_version`,
    `klines_h6`.`source` AS `source` from `klines_h6`;

CREATE OR REPLACE ALGORITHM=UNDEFINED
  DEFINER=`petrosa_crypto`@`%` SQL SECURITY DEFINER VIEW `klines_8h` AS
  select `klines_h8`.`id` AS `id`,`klines_h8`.`symbol` AS `symbol`,
    `klines_h8`.`timestamp` AS `timestamp`,`klines_h8`.`open_time` AS `open_time`,
    `klines_h8`.`close_time` AS `close_time`,`klines_h8`.`interval` AS `interval`,
    `klines_h8`.`open_price` AS `open_price`,`klines_h8`.`high_price` AS `high_price`,
    `klines_h8`.`low_price` AS `low_price`,`klines_h8`.`close_price` AS `close_price`,
    `klines_h8`.`volume` AS `volume`,`klines_h8`.`quote_asset_volume` AS `quote_asset_volume`,
    `klines_h8`.`number_of_trades` AS `number_of_trades`,
    `klines_h8`.`taker_buy_base_asset_volume` AS `taker_buy_base_asset_volume`,
    `klines_h8`.`taker_buy_quote_asset_volume` AS `taker_buy_quote_asset_volume`,
    `klines_h8`.`price_change` AS `price_change`,
    `klines_h8`.`price_change_percent` AS `price_change_percent`,
    `klines_h8`.`extracted_at` AS `extracted_at`,
    `klines_h8`.`extractor_version` AS `extractor_version`,
    `klines_h8`.`source` AS `source` from `klines_h8`;

CREATE OR REPLACE ALGORITHM=UNDEFINED
  DEFINER=`petrosa_crypto`@`%` SQL SECURITY DEFINER VIEW `klines_12h` AS
  select `klines_h12`.`id` AS `id`,`klines_h12`.`symbol` AS `symbol`,
    `klines_h12`.`timestamp` AS `timestamp`,`klines_h12`.`open_time` AS `open_time`,
    `klines_h12`.`close_time` AS `close_time`,`klines_h12`.`interval` AS `interval`,
    `klines_h12`.`open_price` AS `open_price`,`klines_h12`.`high_price` AS `high_price`,
    `klines_h12`.`low_price` AS `low_price`,`klines_h12`.`close_price` AS `close_price`,
    `klines_h12`.`volume` AS `volume`,`klines_h12`.`quote_asset_volume` AS `quote_asset_volume`,
    `klines_h12`.`number_of_trades` AS `number_of_trades`,
    `klines_h12`.`taker_buy_base_asset_volume` AS `taker_buy_base_asset_volume`,
    `klines_h12`.`taker_buy_quote_asset_volume` AS `taker_buy_quote_asset_volume`,
    `klines_h12`.`price_change` AS `price_change`,
    `klines_h12`.`price_change_percent` AS `price_change_percent`,
    `klines_h12`.`extracted_at` AS `extracted_at`,
    `klines_h12`.`extractor_version` AS `extractor_version`,
    `klines_h12`.`source` AS `source` from `klines_h12`;
