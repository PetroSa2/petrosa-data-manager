# Cross-Subscriber Integration Tests (P0.2e)

Round-trip integration tests exercising the full audit-trail chain across
the three subscriber collections owned by `petrosa-data-manager`:

```
cio.intent.>           → intents          (P0.2a)
signals.trading.>      → cio_decisions    (P0.2b)
execution.events.<sid> → execution_events (P0.2c)
pnl.events.<sid>       → pnl_events       (P0.2d — deferred to P4.1)
```

## Approach

The default test (`test_cross_subscriber_audit_trail.py`) uses an **in-process
simulator stack** — no real NATS, MongoDB, Tempo, or Loki are required:

- **Mock NATS broker** — pub/sub in a single asyncio event loop. The broker
  routes messages to subscriber callbacks based on subject matching (`>`
  wildcard supported).
- **In-memory Mongo adapter** — captures `insert_one(...)` calls per
  collection and surfaces them for assertion.
- **InMemorySpanExporter** — installed as the active OTel SpanProcessor for
  the test, so trace continuity can be asserted without Tempo.
- **Caplog** — captures structured log records to assert that `decision_id`
  is present on each consumer's persistence log line (the in-process
  equivalent of the Loki cross-service join requirement).

The simulator emits in publisher order:

1. `cio.intent.X` → IntentConsumer persists to `intents`
2. `signals.trading.Y` → DecisionConsumer persists to `cio_decisions`
3. `execution.events.Z` → ExecutionEventsConsumer persists to `execution_events`

All three messages carry the same `decision_id` so the audit-trail join
assertion can verify the three documents are linkable.

## Acceptance Criteria Coverage

- **3-leg join (always required)** — `test_three_leg_round_trip_joins_by_decision_id`
- **OTel trace continuity (3-leg)** — `test_three_leg_trace_continuity_single_trace_id`
- **Loki cross-service join (3-leg)** — `test_three_leg_logs_carry_decision_id`
- **Round-trip latency** — every test asserts wall time < 5 seconds
- **CI reproducibility** — no external dependencies; runs under `make test`

## 4-leg (P0.2d) test

Deferred per the issue: the `pnl.events.<sid>` leg lands in P4.1. When the
PnL consumer ships, extend `test_cross_subscriber_audit_trail.py` with a
4-leg variant (publisher + assertion) and gate on the new consumer's
existence.

## Optional `@requires-cluster` variant

A future variant that exercises real NATS + MongoDB inside a docker-compose
stack would be added under `tests/integration/round-trip/` with a
`compose.yml` and a `pytest.mark.requires_cluster` gate. The in-process
simulator above is sufficient for the audit-trail contract; the cluster
variant would only catch transport-layer regressions.
