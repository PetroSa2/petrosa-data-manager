-- Operator-run rollback for 016_signals_point_in_time.sql.
-- Capture SHOW CREATE TABLE signals before and after this file.
-- SHOW CREATE TABLE signals;
--
-- One statement, one table rebuild, like the forward file: the unique index and the added columns that exist are
-- dropped by a single ALTER TABLE, run in place without blocking writes (ALGORITHM=INPLACE, LOCK=NONE).
-- Rerunnable: only what exists is dropped, and when nothing is left no ALTER runs.
SET SESSION group_concat_max_len = 8192;
SET @signals_rollback_clauses := (
    SELECT GROUP_CONCAT(added.ddl ORDER BY added.ord SEPARATOR ', ')
    FROM (
        SELECT 1 AS ord, 'i' AS kind, 'uq_signals_signal_key' AS name, 'DROP INDEX uq_signals_signal_key' AS ddl
        UNION ALL SELECT 2, 'c', 'signal_revision_conflicts', 'DROP COLUMN signal_revision_conflicts'
        UNION ALL SELECT 3, 'c', 'last_rejected_payload_hash', 'DROP COLUMN last_rejected_payload_hash'
        UNION ALL SELECT 4, 'c', 'signal_revision_payload_hash', 'DROP COLUMN signal_revision_payload_hash'
        UNION ALL SELECT 5, 'c', 'decision_id', 'DROP COLUMN decision_id'
        UNION ALL SELECT 6, 'c', 'take_profit', 'DROP COLUMN take_profit'
        UNION ALL SELECT 7, 'c', 'stop_loss', 'DROP COLUMN stop_loss'
        UNION ALL SELECT 8, 'c', 'entry_ref_price', 'DROP COLUMN entry_ref_price'
        UNION ALL SELECT 9, 'c', 'bar_close_time', 'DROP COLUMN bar_close_time'
        UNION ALL SELECT 10, 'c', 'bar_open_time', 'DROP COLUMN bar_open_time'
        UNION ALL SELECT 11, 'c', 'signal_key', 'DROP COLUMN signal_key'
    ) AS added
    WHERE (added.kind = 'c' AND added.name IN (
              SELECT column_name FROM information_schema.columns
              WHERE table_schema = DATABASE() AND table_name = 'signals'))
       OR (added.kind = 'i' AND added.name IN (
              SELECT index_name FROM information_schema.statistics
              WHERE table_schema = DATABASE() AND table_name = 'signals'))
);
SET @signals_rollback_ddl := IF(
    @signals_rollback_clauses IS NULL,
    'SELECT 1',
    CONCAT('ALTER TABLE signals ', @signals_rollback_clauses, ', ALGORITHM=INPLACE, LOCK=NONE')
);
PREPARE signals_rollback_stmt FROM @signals_rollback_ddl;
EXECUTE signals_rollback_stmt;
DEALLOCATE PREPARE signals_rollback_stmt;
-- SHOW CREATE TABLE signals;
