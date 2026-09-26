-- Migration 008A: remove duplicate klines rows before adding UNIQUE keys.
--
-- Keep-one rule: for each (symbol, timestamp), retain the row with the greatest
-- extracted_at; ties are resolved by the lexicographically lowest id.  The
-- procedure deletes at most 1,000 rows per statement, so each transaction is
-- bounded for the shared DBaaS wait_timeout.  This migration is idempotent:
-- after the first run every subsequent pass deletes zero rows.
--
-- This script is operator-gated.  It must run before 008_klines_m1_m15_unique.sql.

DELIMITER $$

CREATE PROCEDURE dedupe_008_klines_m1_m15()
BEGIN
    DECLARE rows_deleted BIGINT DEFAULT 1;

    WHILE rows_deleted > 0 DO
        DELETE FROM klines_m1
        WHERE id IN (
            SELECT doomed_id
            FROM (
                SELECT victim.id AS doomed_id
                FROM klines_m1 AS victim
                INNER JOIN klines_m1 AS survivor
                  ON survivor.symbol = victim.symbol
                 AND survivor.timestamp = victim.timestamp
                 AND (
                      survivor.extracted_at > victim.extracted_at
                      OR (
                          survivor.extracted_at = victim.extracted_at
                          AND survivor.id < victim.id
                      )
                 )
                LIMIT 1000
            ) AS bounded_m1
        );
        SET rows_deleted = ROW_COUNT();
    END WHILE;

    SET rows_deleted = 1;
    WHILE rows_deleted > 0 DO
        DELETE FROM klines_m15
        WHERE id IN (
            SELECT doomed_id
            FROM (
                SELECT victim.id AS doomed_id
                FROM klines_m15 AS victim
                INNER JOIN klines_m15 AS survivor
                  ON survivor.symbol = victim.symbol
                 AND survivor.timestamp = victim.timestamp
                 AND (
                      survivor.extracted_at > victim.extracted_at
                      OR (
                          survivor.extracted_at = victim.extracted_at
                          AND survivor.id < victim.id
                      )
                 )
                LIMIT 1000
            ) AS bounded_m15
        );
        SET rows_deleted = ROW_COUNT();
    END WHILE;
END$$

DELIMITER ;

CALL dedupe_008_klines_m1_m15();
DROP PROCEDURE dedupe_008_klines_m1_m15;

-- Separate cleanup for the known garbage row.  Both predicates are required:
-- an empty symbol alone is not safe because valid rows may use that timestamp.
DELETE FROM klines_m5
WHERE symbol = ''
  AND timestamp = '0000-00-00 00:00:00';
