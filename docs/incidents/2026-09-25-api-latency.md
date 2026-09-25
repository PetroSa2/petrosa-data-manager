# Data-manager API latency incident — 2026-09-25

## Finding

The API and consumers share the event loop. `DatabaseManager.initialize()` called
the synchronous SQLAlchemy/MySQL `connect()` method directly from `async def`, and
the reconnection and shutdown paths made the same mistake (`data_manager/db/database_manager.py:78-95,
128-139, 241-254`). A slow connection or table-creation operation therefore paused
FastAPI requests, matching the measured multi-second `/openapi.json` and performance
latencies while CPU remained low.

The generic MySQL gateway also called synchronous `write()`/`update()`/`query_range()`
from async request handlers (`data_manager/api/routes/generic.py:576-592,
664-713, 735-739, 878-916`). Those calls now run through `asyncio.to_thread`, so
the request path remains responsive while the bounded synchronous adapter work runs
in a worker thread.

## Fix and evidence

MySQL connect, reconnect, and disconnect are offloaded with `asyncio.to_thread`.
Generic MySQL writes, updates, and reads use the same mechanism. MongoDB access was
already asynchronous through Motor. The new opt-in `DM_LOOP_LAG_MONITOR=true`
background task in `data_manager/utils/event_loop_monitor.py` logs
`EVENT_LOOP_LAG ms=<n>` at WARNING for pauses above 500 ms and emits the loop-thread
stack no more than once per minute.

The monitor is disabled by default. Unit tests cover the disabled/default behavior,
healthy scheduling, lag warnings, and stack-report rate limiting.

## Operational follow-up

Enable `DM_LOOP_LAG_MONITOR=true` temporarily during a controlled production check
to confirm lag remains below 500 ms, then disable it. Splitting consumers into a
separate deployment remains a future architectural option and is out of scope.
