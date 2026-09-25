# Persistence architecture

## Rule

MongoDB is transient service storage. MySQL is the durable system of record
for business events and history. A MongoDB collection must be registered in
`data_manager/persistence_registry.py` before application code writes it.

The registry is machine-readable. It records the durable MySQL table and
logical key where that copy exists, or explains why a collection is
transient-only. Entries marked `pending=True` are durable business data whose
MySQL writer belongs to a follow-up W-ticket; they are still protected from
destructive maintenance now.

## Current classifications

The durable audit streams are `signals`, `cio_decisions`, `execution_events`,
`pnl_events`, `intents`, `alerts`, `trades`, and `funding_rates_*`. The latter
three pending writers are recorded explicitly so a maintenance change cannot
mistake an unwritten durable target for disposable data.

The pending dual-write work belongs to the epic's W1/W9/W10 follow-up tickets.
Until those writers land, the registry keeps the corresponding MySQL table
name and key visible and protects the table from the orphan-drop migration.

`leader_election`, `distributed_locks`, `config_rate_limits`, and diagnostic
probe collections are transient coordination or cache state. `candles_*`,
`klines_*`, and `analytics_*` are recomputable; MySQL klines are the durable
market-data source. Dynamic names are covered only by explicit prefixes in
the registry, not by an unrestricted wildcard.

## Adding a collection

1. Add an exact entry with `durable(mysql_table=..., key=...)` or
   `transient_only(reason=...)`.
2. Use a prefix entry only when the collection name is intentionally dynamic,
   and document why every name with that prefix has the same policy.
3. Add or update a registry test and, if the table is durable, ensure no
   destructive maintenance job includes it in `TARGET_TABLES`.
4. Run `pytest tests/test_persistence_registry.py` and the maintenance tests.

The CI test scans `data_manager/` for collection-name constants and static
`db[...]` accesses. It includes a fixture that writes an unregistered name,
so an absent registry entry fails loudly instead of becoming an undocumented
storage dependency.
