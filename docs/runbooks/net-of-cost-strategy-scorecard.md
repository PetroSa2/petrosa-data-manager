# Net-of-Cost Strategy Scorecard Verification

This procedure is read-only. Use a current closed UTC interval and retain the exact response bodies with UTC request
timestamps; do not call write, repair, migration, or mutation endpoints.

## Procedure

1. Choose `FROM` and `TO` in UTC, with `TO` after the most recent fully closed candle or execution window.
2. Record the UTC timestamp, URL, HTTP status, and response body for:

```bash
curl --fail-with-body -sS -H 'Accept: application/json' \
  "https://data-manager.example/analysis/scorecard?from=${FROM}&to=${TO}&group_by=strategy"
curl --fail-with-body -sS -H 'Accept: application/json' \
  "https://data-manager.example/analysis/scorecard/evaluate?from=${FROM}&to=${TO}"
```

3. Confirm the scorecard response includes `period`, `status`, `source_errors`, and one group per reported strategy.
4. For every group, record `net_pnl`, `fees`, `funding_allocated`, `trade_count` or `n_trades`, `sample_size` or
   `sample_ok`, and `uncertainty` intervals. The `net_of_cost.formula` must state that fees and funding are subtracted.
5. Accept policy statuses only when the evaluation response is `configured`, every group is structurally complete, and
   the source status is `complete`. `unavailable`, `incomplete`, or missing fields are evidence of no decision.
6. Compare the response `period` and source timestamps with the selected UTC range, then retain the response evidence
   with the verification timestamp.

No production data is written by these requests.
