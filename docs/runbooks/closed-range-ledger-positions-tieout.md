# Closed-Range Ledger and Positions Tie-Out

This verification is read-only. It uses only `GET` requests and does not insert,
update, delete, or repair ledger, position, or analytics data.

## Procedure

1. Choose a completed UTC day, `D`, at least one day before the current UTC date.
   Use the same day for both endpoints and record the selection timestamp in UTC.
2. Request the closed-range ledger comparison:

   ```bash
   curl --fail-with-body --silent --show-error \
     "${DATA_MANAGER_URL}/api/v1/ledger/tieout?from=${D}&to=${D}" \
     | tee "ledger-tieout-${D}.json"
   ```

3. Request the latest exchange/ledger positions comparison:

   ```bash
   curl --fail-with-body --silent --show-error \
     "${DATA_MANAGER_URL}/api/v1/ledger/positions-tieout" \
     | tee "positions-tieout-${D}.json"
   ```

4. Record the response URL, UTC request timestamps, HTTP status, and complete
   response bodies in the verification record. Do not call any `PUT`, `POST`,
   `PATCH`, or `DELETE` endpoint.

## Evidence Requirements

The ledger response must include the requested `from` and `to`, per-day status,
`unexplained`, `cumulative_variance`, `incomplete`, and `source_status`. Treat
`incomplete: true`, an `empty` source, HTTP 503, or a missing response as a
failed/incomplete verification, not as a zero remainder or a match.

The positions response must include `as_of_ms`, `source_status`, `incomplete`,
and every `quantity_tieout` row. Record each `(symbol, position_side)` with its
`ledger_open_quantity`, `exchange_quantity`, `difference`, and `status`.
An empty `quantity_tieout` is not evidence of a match unless both required
sources were read and the response explicitly establishes a complete result.

## No-Write Check

Review the captured request history and application audit logs for the same
window. The evidence is acceptable only when no write endpoint or database
mutation was issued during this verification.
