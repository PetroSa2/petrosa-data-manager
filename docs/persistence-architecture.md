# Persistence architecture

## Rule

MongoDB is the operational store: every live-path read and write goes to MongoDB. MySQL holds a historic reference copy only (statistical analysis, backtesting, research) and is never read on the live path.

`petrosa-data-manager` is the only service that connects to any database; every other service reads and writes data exclusively through the data-manager API.

A MongoDB collection must be registered in `data_manager/persistence_registry.py`
before application code writes it.

The registry is machine-readable. `durable` means Mongo operational data with a
MySQL historic copy. `operational` means Mongo live-path data protected from
drop and orphan jobs, with an optional MySQL historic copy. `transient_only`
means cache, coordination, or recomputable state. Entries marked `pending=True`
are durable business data whose MySQL writer belongs to a follow-up W-ticket;
they are still protected from destructive maintenance now.

## Current classifications

The durable audit streams are `signals`, `cio_decisions`, `execution_events`,
`pnl_events`, `intents`, `alerts`, `trades`, and `funding_rates_*`. The latter
three pending writers are recorded explicitly so a maintenance change cannot
mistake an unwritten durable target for disposable data.

The pending dual-write work belongs to the epic's W1/W9/W10 follow-up tickets.
Until those writers land, the registry keeps the corresponding MySQL table
name and key visible and protects the table from the orphan-drop migration.

`leader_election`, `distributed_locks`, `config_rate_limits`, and diagnostic
probe collections are transient coordination or cache state. `candles_*` and
`analytics_*` are recomputable; `klines_*` is the operational candle store,
with MySQL klines as its historic copy. Dynamic names are covered only by
explicit prefixes in the registry, not by an unrestricted wildcard.

## Adding a collection

1. Add an exact entry with `durable(mysql_table=..., key=...)`,
   `operational(reason=...)`, or `transient_only(reason=...)`.
2. Use a prefix entry only when the collection name is intentionally dynamic,
   and document why every name with that prefix has the same policy.
3. Add or update a registry test and, if the table is durable, ensure no
   destructive maintenance job includes it in `TARGET_TABLES`.
4. Run `pytest tests/test_persistence_registry.py` and the maintenance tests.

The CI test scans `data_manager/` for collection-name constants and static
`db[...]` accesses. It includes a fixture that writes an unregistered name,
so an absent registry entry fails loudly instead of becoming an undocumented
storage dependency.
