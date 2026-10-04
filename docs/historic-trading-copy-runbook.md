# Historic trading copy runbook

`copy_historic_trading_history` copies the MongoDB `execution_events`, `trades`,
and symbol-suffixed `trades_*` collections into the durable MySQL tables.

The command is a dry run unless `--apply` is supplied. A dry run reports rows
per UTC day and invalid documents without changing either database.

```bash
uv run python -m data_manager.maintenance.copy_historic_trading_history \
  --batch-size 500 \
  --checkpoint /tmp/trading-history-copy.json
```

Review the per-day output, then repeat with `--apply`. The checkpoint is
updated after each applied batch and can be used to resume an interrupted run.
Use `--collection execution_events` or `--collection trades` to run one group.

The MySQL writes use the existing durable-table unique keys, so rerunning a
completed range is idempotent. After the copy, run the proof for each group:

```bash
uv run python -m data_manager.maintenance.historic_copy_proof \
  --collection execution_events
uv run python -m data_manager.maintenance.historic_copy_proof \
  --collection trades
```

The proof must return an empty `failures` list for both collections before any
retention policy is enabled.
