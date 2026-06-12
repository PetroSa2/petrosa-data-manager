-- Migration 003: drop unused open_time index from all klines tables
-- Ticket: PetroSa2/petrosa-data-manager#230
-- Reason: No application query filters on open_time alone; (symbol, timestamp) composite index covers all reads
-- Verified: grep -r "open_time" shows no query filters, only MongoDB fallback candidate list

DROP INDEX IF EXISTS idx_klines_m5_open_time ON klines_m5;
DROP INDEX IF EXISTS idx_klines_m15_open_time ON klines_m15;
DROP INDEX IF EXISTS idx_klines_m30_open_time ON klines_m30;
DROP INDEX IF EXISTS idx_klines_h1_open_time ON klines_h1;
DROP INDEX IF EXISTS idx_klines_d1_open_time ON klines_d1;
