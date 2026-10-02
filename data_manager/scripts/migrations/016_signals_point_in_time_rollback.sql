-- Operator-run rollback for 016_signals_point_in_time.sql.
-- Capture SHOW CREATE TABLE signals before and after this file.
-- SHOW CREATE TABLE signals;
ALTER TABLE signals
    DROP INDEX uq_signals_signal_key,
    DROP COLUMN signal_revision_conflicts,
    DROP COLUMN last_rejected_payload_hash,
    DROP COLUMN signal_revision_payload_hash,
    DROP COLUMN decision_id,
    DROP COLUMN take_profit,
    DROP COLUMN stop_loss,
    DROP COLUMN entry_ref_price,
    DROP COLUMN bar_close_time,
    DROP COLUMN bar_open_time,
    DROP COLUMN signal_key;
