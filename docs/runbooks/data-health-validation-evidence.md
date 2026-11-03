# DataHealthMetrics validation evidence

Use the Prometheus API with GET-only access after every data-manager rollout. Query
the same UTC range for all data-manager containers; Prometheus retains samples when
pods are replaced, and `increase` handles counter resets between containers.

## Required 24-hour query

Set `START` and `END` to the exact UTC boundaries of the closed window, then run:

```promql
sum by (symbol, timeframe) (
  increase(data_manager_health_metrics_validation_checks_total{job=~".*data-manager.*"}[24h])
)
```

The matching validation-failure query is:

```promql
sum by (symbol, timeframe) (
  increase(data_manager_health_metrics_validation_errors_total{job=~".*data-manager.*"}[24h])
)
```

Acceptance requires every returned validation-error value to be `0` and each
covered symbol/timeframe to have a successful-check count greater than `0`.
Record the exact UTC range, query expressions, returned series, and the live image.

## Classification

`data_manager_health_metrics_validation_errors_total` and the log marker
`data_health_metrics_validation_error` are validation failures only. Do not count
transport timeouts or analytics warnings as validation failures.

The separate thin-data evidence metric retains those categories by `error_type`:

```promql
sum by (consumer, error_type, symbol, timeframe) (
  increase(data_manager_thin_data_consumer_errors_total{job=~".*data-manager.*"}[24h])
)
```

Use the returned `error_type` values to report transport or analytics warnings
separately from the validation-error result. The queries are observational and do
not write to Kubernetes, Prometheus, MongoDB, MySQL, or production data.
