-- Migration 005: refresh InnoDB cardinality stats after migrations 002-004
-- Ticket: PetroSa2/petrosa-data-manager#233
-- Reason: After dropping and adding indexes (migrations 002-004) and
--         tradeengine#466, InnoDB statistics are stale. ANALYZE TABLE
--         refreshes optimizer stats so the correct indexes are chosen.
-- NOTE: ANALYZE TABLE is fast (seconds–minutes) and non-blocking for reads.
-- Pre-condition: All migrations 002-004 AND petrosa-tradeengine#466 deployed.

ANALYZE TABLE audit_logs, health_metrics, backfill_jobs;
ANALYZE TABLE klines_m5, klines_m15, klines_m30, klines_h1, klines_d1;
