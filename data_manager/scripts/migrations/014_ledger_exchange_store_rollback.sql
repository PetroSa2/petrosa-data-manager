-- Rehearsal rollback is intentionally non-destructive. Ledger data is permanent history;
-- operators may disable the routes, but must not drop or delete the durable tables.
SELECT 'ledger exchange rollback is a no-op; durable history retained' AS rollback_note;
