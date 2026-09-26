# Data-manager API latency incident — 2026-09-25

Ticket: PetroSa2/petrosa-data-manager#370.

## Symptom

`GET /analysis/performance/{strategy_id}` took 5–14 s while its Mongo query
took 0.14 s. `/openapi.json` (5.3 s) and `/health` (3.4 s) were just as slow,
and the pod used about 80m of CPU. That pattern means a blocked event loop:
`python -m data_manager.main` runs the API, the NATS consumers and the
schedulers on one asyncio loop, so any synchronous call inside an `async def`
holds up every request.

## Blockers

These were found by reading the code paths that live traffic reaches, not from
monitor output; the monitor described below ships in the same change. Each
one is a synchronous MySQL call (SQLAlchemy + PyMySQL, with connect, read and
write timeouts of `DB_CONNECTION_TIMEOUT`) made directly on the event loop.
Line numbers point at the fixed call sites.

| # | Call site | Blocking call | Reached by |
|---|---|---|---|
| 1 | `data_manager/db/database_manager.py:261` (`_connect_mysql_adapter`, used by `initialize()` at `:93` and `_reconnect_mysql()` at `:281`) | `MySQLAdapter.connect()`: engine creation, `SELECT 1`, `metadata.create_all` | Startup (the API is already serving while databases initialize). Since #400, also every `DB_HEALTH_CHECK_INTERVAL` (30 s) while MySQL is unavailable, until `DB_RECONNECT_MAX_ATTEMPTS` |
| 2 | `data_manager/db/database_manager.py:138` (`shutdown()`) | `MySQLAdapter.disconnect()` | Shutdown |
| 3 | `data_manager/api/routes/generic.py:201` (`_dual_write_signals_to_mysql`) | `write(records, "signals")` | Every `POST /api/v1/mongodb/signals` |
| 4 | `data_manager/api/routes/generic.py:623` (`insert_records`) | `write()` | `POST /api/v1/mysql/{collection}` |
| 5 | `data_manager/api/routes/generic.py:714`, `:757`, `:788` (`update_records`) | `query_range()` over the whole table, then `update()` or `write()` | `PUT /api/v1/mysql/{collection}` (positions, daily_pnl) |
| 6 | `data_manager/api/routes/generic.py:843` (`delete_records`) | `query_range()` over the whole table | `DELETE /api/v1/mysql/{collection}` |
| 7 | `data_manager/api/routes/generic.py:929`, `:944`, `:968` (`batch_operations`) | `write()`, `query_range()` | `POST /api/v1/mysql/{collection}/batch` |

On 2026-09-26 the live pod was still serving `/api/v1/mysql/positions` and
`/api/v1/mongodb/signals` (seen in `gateway_auth_unverified` log lines), so #3
and #5 were still on the request path after #399 and #400. `update_records`
reads the whole table with `query_range(datetime.min, datetime.max)` and
filters in Python, so its cost grows with the table.

## Fix

Every call above now runs in a worker thread through `asyncio.to_thread`.

- MySQL connects go through `DatabaseManager._connect_mysql_adapter()`. It
  returns the new adapter only after `connect()` has finished, and only then
  is it assigned to `mysql_adapter`. While the thread runs, other coroutines
  see the previous adapter (or None), never one whose tables are still being
  defined.
- The signals dual-write stays fire-and-forget, so the response does not wait
  for MySQL. Its task is kept in `_signal_dual_write_tasks` until it finishes.
  Write errors are logged in the worker (`MySQL signals dual-write failed`)
  instead of surfacing only as unretrieved task exceptions.
- Response shapes are unchanged. `/analysis/performance/*` is not touched.

`tests/test_370_offloaded_mysql_calls.py` checks that each call site runs off
the loop. It also checks that `/health/liveness` and `/openapi.json` answer in
under 1 s while 2 s MySQL calls (a reconnect, a generic MySQL write and a
signals dual-write) are in flight.

## Diagnostics: `DM_LOOP_LAG_MONITOR`

The monitor is off by default. With `DM_LOOP_LAG_MONITOR=true`,
`data_manager/utils/event_loop_monitor.py` starts first thing in
`DataManagerApp.start()` (`data_manager/main.py:174`), so it also sees
startup stalls. It has two parts:

- a coroutine that sleeps 0.5 s and logs `EVENT_LOOP_LAG ms=<n>` at WARNING
  when a wake-up is at least 500 ms late;
- a watchdog thread that, while the loop is blocked, logs the loop thread's
  stack as `EVENT_LOOP_LAG stack (loop blocked <n> ms)`, at most once a
  minute.

The stack has to be sampled from another thread. Once the loop runs again, the
blocking code has already returned, and the loop thread's own stack shows
only the monitor.

## Remaining synchronous MySQL calls on the loop (follow-up)

These are outside the paths above and are left for a follow-up:

- The MySQL fallback read and insert paths in
  `data_manager/db/repositories/candle_repository.py` (`_read_fallback_range`,
  `_read_fallback_latest`, `insert`). Candles have been served from Mongo
  since #374 and #401.
- The MySQL paths in `data_manager/db/repositories/schema_repository.py` and
  `catalog_repository.py`.
- `backfill_pair` → `query_latest` in
  `data_manager/maintenance/candle_warmup_backfill.py`, which runs in-process
  through `CandleWarmupScheduler` when `ENABLE_CANDLE_WARMUP_SCHEDULER=true`.
  That flag is off by default and in the current manifests.

All offloaded MySQL work shares the event loop's default thread pool
(`min(32, cpu + 4)` workers). A hung MySQL now queues work there instead of
freezing the API.

## Operational follow-up

- Set `DM_LOOP_LAG_MONITOR=true` in production for a while to confirm lag
  stays under 500 ms, then turn it off. Any stack it logs names the next
  blocker.
- Splitting the consumers into their own Deployment is still an option; it is
  out of scope here.
