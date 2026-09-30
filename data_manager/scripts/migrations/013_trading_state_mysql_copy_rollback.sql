-- PetroSa2/petrosa-data-manager#464 rollback rehearsal artifact.
-- Preserve all additive columns and dead letters. This rollback only restores the
-- prior enum vocabulary after an operator has independently verified no newer
-- status values remain; it never deletes data or schema.
ALTER TABLE positions MODIFY status ENUM('open','partially_closed','closed') NULL;
