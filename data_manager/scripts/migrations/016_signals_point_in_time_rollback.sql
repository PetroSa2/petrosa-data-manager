-- Operator-run rollback for 016_signals_point_in_time.sql.
-- Capture SHOW CREATE TABLE signals before and after this file.
-- SHOW CREATE TABLE signals;
SET @signals_rollback_ddl := IF(
    (SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema = DATABASE() AND table_name = 'signals' AND index_name = 'uq_signals_signal_key') > 0,
    'ALTER TABLE signals DROP INDEX uq_signals_signal_key',
    'SELECT 1'
);
PREPARE signals_rollback_stmt FROM @signals_rollback_ddl;
EXECUTE signals_rollback_stmt;
DEALLOCATE PREPARE signals_rollback_stmt;

SET @signals_rollback_ddl := IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'signals' AND column_name = 'signal_revision_conflicts') > 0,
    'ALTER TABLE signals DROP COLUMN signal_revision_conflicts',
    'SELECT 1'
);
PREPARE signals_rollback_stmt FROM @signals_rollback_ddl;
EXECUTE signals_rollback_stmt;
DEALLOCATE PREPARE signals_rollback_stmt;

SET @signals_rollback_ddl := IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'signals' AND column_name = 'last_rejected_payload_hash') > 0,
    'ALTER TABLE signals DROP COLUMN last_rejected_payload_hash',
    'SELECT 1'
);
PREPARE signals_rollback_stmt FROM @signals_rollback_ddl;
EXECUTE signals_rollback_stmt;
DEALLOCATE PREPARE signals_rollback_stmt;

SET @signals_rollback_ddl := IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'signals' AND column_name = 'signal_revision_payload_hash') > 0,
    'ALTER TABLE signals DROP COLUMN signal_revision_payload_hash',
    'SELECT 1'
);
PREPARE signals_rollback_stmt FROM @signals_rollback_ddl;
EXECUTE signals_rollback_stmt;
DEALLOCATE PREPARE signals_rollback_stmt;

SET @signals_rollback_ddl := IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'signals' AND column_name = 'decision_id') > 0,
    'ALTER TABLE signals DROP COLUMN decision_id',
    'SELECT 1'
);
PREPARE signals_rollback_stmt FROM @signals_rollback_ddl;
EXECUTE signals_rollback_stmt;
DEALLOCATE PREPARE signals_rollback_stmt;

SET @signals_rollback_ddl := IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'signals' AND column_name = 'take_profit') > 0,
    'ALTER TABLE signals DROP COLUMN take_profit',
    'SELECT 1'
);
PREPARE signals_rollback_stmt FROM @signals_rollback_ddl;
EXECUTE signals_rollback_stmt;
DEALLOCATE PREPARE signals_rollback_stmt;

SET @signals_rollback_ddl := IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'signals' AND column_name = 'stop_loss') > 0,
    'ALTER TABLE signals DROP COLUMN stop_loss',
    'SELECT 1'
);
PREPARE signals_rollback_stmt FROM @signals_rollback_ddl;
EXECUTE signals_rollback_stmt;
DEALLOCATE PREPARE signals_rollback_stmt;

SET @signals_rollback_ddl := IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'signals' AND column_name = 'entry_ref_price') > 0,
    'ALTER TABLE signals DROP COLUMN entry_ref_price',
    'SELECT 1'
);
PREPARE signals_rollback_stmt FROM @signals_rollback_ddl;
EXECUTE signals_rollback_stmt;
DEALLOCATE PREPARE signals_rollback_stmt;

SET @signals_rollback_ddl := IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'signals' AND column_name = 'bar_close_time') > 0,
    'ALTER TABLE signals DROP COLUMN bar_close_time',
    'SELECT 1'
);
PREPARE signals_rollback_stmt FROM @signals_rollback_ddl;
EXECUTE signals_rollback_stmt;
DEALLOCATE PREPARE signals_rollback_stmt;

SET @signals_rollback_ddl := IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'signals' AND column_name = 'bar_open_time') > 0,
    'ALTER TABLE signals DROP COLUMN bar_open_time',
    'SELECT 1'
);
PREPARE signals_rollback_stmt FROM @signals_rollback_ddl;
EXECUTE signals_rollback_stmt;
DEALLOCATE PREPARE signals_rollback_stmt;

SET @signals_rollback_ddl := IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'signals' AND column_name = 'signal_key') > 0,
    'ALTER TABLE signals DROP COLUMN signal_key',
    'SELECT 1'
);
PREPARE signals_rollback_stmt FROM @signals_rollback_ddl;
EXECUTE signals_rollback_stmt;
DEALLOCATE PREPARE signals_rollback_stmt;
