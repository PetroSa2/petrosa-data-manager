# Candle consumer / retention-window contract

Authoritative contract for how many candles the Mongo `candles_{pair}_{period}`
execution hot-path must retain per timeframe. Produced by
[`PetroSa2/petrosa-data-manager#276`](https://github.com/PetroSa2/petrosa-data-manager/issues/276)
to unblock the window-sizing decisions in
[`#274`](https://github.com/PetroSa2/petrosa-data-manager/issues/274) (AC3 retention) and
[`#275`](https://github.com/PetroSa2/petrosa-data-manager/issues/275) (AC1 warm-up backfill
depth). Related epic [`PetroSa2/petrosa_k8s#783`](https://github.com/PetroSa2/petrosa_k8s/issues/783);
candle-backend audit [`petrosa_k8s#794`](https://github.com/PetroSa2/petrosa_k8s/issues/794).

## AC1 — Consumer inventory

Only **one** service consumes MongoDB OHLC candle data on the execution path:
**`petrosa-bot-ta-analysis`**. The other two candidate consumers were checked and ruled out:

| Repo | Consumes candles? | Evidence |
|---|---|---|
| `petrosa-bot-ta-analysis` | **Yes** — sole execution-path candle consumer | see table below |
| `petrosa-realtime-strategies` | No — consumes raw trade/ticker/depth ticks only, never OHLC | `strategies/models/market_data.py` has no Kline/Candle model; zero `candle|kline` hits in `strategies/`; every Data Manager query targets `strategy_configs_*`/`strategy_config_audit` collections, never `candles_*`/`klines_*`. |
| `petrosa-tradeengine` | No — consumes signals/intents, positions, config, leverage status, risk envelopes only | one `candle` grep hit is a comment (`exchange/binance.py:1114`), unrelated to data fetch; `services/data_manager_client.py` only ever queries `trading_configs_*`, `trading_configs_audit`, `leverage_status`. |

### `petrosa-bot-ta-analysis` fetch architecture

Candles are fetched **once per NATS message** (not per strategy) and the same DataFrame is
shared across every enabled strategy:

- `ta_bot/services/nats_listener.py:241` — `fetch_candles(symbol, period, limit=250)` (hardcoded).
- `ta_bot/services/mysql_client.py:180-185` — same `limit: int = 250` default, forwards to Data
  Manager when `USE_DATA_MANAGER` is enabled (default on).
- `ta_bot/services/data_manager_client.py:92-93` — `fetch_candles(..., limit: int = 250)`.
- `ta_bot/core/signal_engine.py:184-218,329` — `analyze_candles()` passes the **same 250-candle
  df** to every strategy in `self.strategies`.
- Timeframes actually requested: `SUPPORTED_TIMEFRAMES` env (sourced from the shared
  `SUPPORTED_INTERVALS` configmap key, `k8s/bot-ta-analysis/deployment.yaml:68-72`) = the full
  `5m,15m,30m,1h,1d` set (`k8s/shared/configmaps/petrosa-common-config.yaml:12`). Ten pairs ×
  five intervals = 50 `candles_{pair}_{period}` collections
  (`SUPPORTED_PAIRS`, `petrosa-common-config.yaml:11`).
- Backtest CLI path (`backtest/data_source.py:94,115-117`,
  `DataManagerHistoricalSource(max_candles=1000)`) is a **separate, offline** consumer — it reads
  historical data for analysis, not the live execution hot path, so it is intentionally excluded
  from the Mongo warm-path sizing below (a historic-tier read, consistent with #274 AC2's
  hot/cold split).

### Per-strategy max lookback (live path only, `ta_bot/strategies/*`)

The true dependency is the indicator window each strategy needs, independent of the current
(buggy — see callout) 250-candle fetch limit:

| Strategy | Max lookback (candles) | Reference |
|---|---|---|
| `minervini_trend_template` | **265** | `min_periods=265`; `closes.rolling(200)` + 52-wk (260-bar) window — see bug callout below |
| `multi_timeframe_trend_continuation` | 200 | engine-shared EMA200 |
| `ichimoku_cloud_momentum` | 78 (effective) | `rolling(52)` then `.shift(26)` — guard only checks `<52`, a separate correctness bug, not a fetch-limit issue |
| `bear_trap_buy` / `bear_trap_sell` / `ema_alignment_bearish` / `inside_bar_sell` / `inside_bar_breakout` / `fox_trap_reversal` | 80 | EMA80 |
| `ema_alignment_bullish` | 80 | `ewm(span=80)` |
| `golden_trend_sync` | 50 | EMA50 |
| `ema_pullback_continuation` | 30 | `ewm(span=30)` |
| `momentum_pulse` | 26 | engine MACD slow=26 |
| `divergence_trap` | 14 | RSI(14) |
| `bollinger_squeeze_alert` / `bollinger_breakout_signals` / `band_fade_reversal` / `mean_reversion_scalper` / `volume_surge_breakout` / `range_break_pop` / `shooting_star_reversal` | 20 | BB(20) / SMA(20) window |
| `doji_reversal` | 10 | `rolling(10)` |
| `liquidity_grab_reversal` | 10 | `lookback_periods=10` |
| `order_flow_imbalance` | 10 | `volume_window=10` |
| `ema_momentum_reversal` / `ema_slope_reversal_sell` | 9 | EMA9 |
| `hammer_reversal_pattern` | 5 | pattern window |
| `rsi_extreme_reversal` | 2 | RSI(2) — self-filtered to 15m/30m/1h only |

**True maximum across all live strategies: 265 candles** (`minervini_trend_template`).

> **Known bug, out of scope for this ticket:** `minervini_trend_template`'s `min_periods=265`
> guard is never satisfiable because every upstream fetch call is hardcoded to `limit=250`
> (`nats_listener.py:241`, `mysql_client.py:181`, `data_manager_client.py:93`) — the strategy can
> never fire in production today. A second bug is masked by the first: the strategy's
> unreachable branch calls `self.indicators`/`self.logger`, neither of which is set in
> `__init__` (unlike sibling strategies), so fixing the fetch-limit bug alone would immediately
> surface an `AttributeError`. **This retention contract sizes for the strategy's documented
> 265-candle requirement regardless of the bug**, so the Mongo warm path is not under-provisioned
> the day someone fixes `ta-bot`'s fetch limit. File a `petrosa-bot-ta-analysis` bug ticket for
> the fetch-limit-vs-`min_periods` mismatch and the missing `self.indicators`/`self.logger` init
> separately — it is a ta-bot code fix, not a data-manager retention concern.

## AC2 — Per-timeframe window table

Because `petrosa-bot-ta-analysis` requests the **same candle count regardless of timeframe**
(`limit=250` is timeframe-agnostic today, and the true strategy dependency of 265 is likewise
timeframe-agnostic), the derived window is a **uniform candle count** across all five
`SUPPORTED_INTERVALS`, not a per-timeframe day budget. This is a deliberate design choice: sizing
by candle-count (not calendar days) keeps storage bounded and predictable regardless of how many
timeframes or pairs are added later — a 1d candle costs the same one document as a 5m candle.

**Safety margin:** 1.5× the true documented maximum (265), rounded up to a clean number:
`265 × 1.5 = 397.5 → 400 candles`. Rationale for the margin:

- Headroom for the `minervini_trend_template` bug fix landing without an immediate follow-up
  retention change.
- Headroom for missing/duplicate candles from extractor lag (occasional gaps mean "265 most
  recent" isn't always "265 candles wide" in wall-clock terms).
- Headroom for near-term new strategies (up to ~400-candle lookback) without re-touching this
  contract, honoring AC5's "future strategy additions update it deliberately" — additions **above**
  400 must update this doc; additions below it are automatically covered.

| Timeframe | Min candle count | Equivalent days (400 × interval) |
|---|---|---|
| `5m`  | 400 | ~1.4 days (33.3 h) |
| `15m` | 400 | ~4.2 days (100 h) |
| `30m` | 400 | ~8.3 days (200 h) |
| `1h`  | 400 | ~16.7 days |
| `1d`  | 400 | ~13.2 months |

`MIN_WARMUP_CANDLES = 400` applies uniformly to every `(pair, timeframe)` pair in
`SUPPORTED_PAIRS × SUPPORTED_INTERVALS` (10 × 5 = 50 collections,
`k8s/shared/configmaps/petrosa-common-config.yaml:11-12`).

This is the number **#275 AC1** (warm-up backfill depth) and **#274 AC3** (Mongo retention/TTL)
should both consume — retention must be at least as deep as the warm-up target, or a freshly
cut-over collection would start shedding candles it just backfilled.

## AC3 — Quota sanity check

Per-document footprint, from `data_manager/models/market_data.py` `Candle` (fields: `symbol`,
`timestamp`, `open`/`high`/`low`/`close`/`volume` as `Decimal128`, optional `quote_volume`,
optional `trades_count`, `timeframe`) plus the two non-`_id` indexes created by
`ensure_indexes()`/`candle_repository.py:230` for any collection name that isn't one of the
special-cased ones (`timestamp` single-field + `(symbol, timestamp)` compound):

- Raw BSON document: ~230 bytes (field names + `Decimal128`×5 @ 16 bytes + `ObjectId` `_id` +
  BSON envelope overhead).
- Index overhead (2 non-default indexes): ~100 bytes/doc (conservative, no WiredTiger compression
  discount applied).
- **~330 bytes/candle**, uncompressed.

At `MIN_WARMUP_CANDLES = 400`:

```
400 candles × 330 bytes           ≈ 132 KB / collection
× 50 collections (10 pairs × 5 tf) ≈ 6.6 MB total steady-state footprint
```

**6.6 MB is ≈1.3% of the Atlas M0 512 MB budget** — the candle-count-based scheme keeps this
namespace two orders of magnitude below the four prior P0 quota outages
([`#783`](https://github.com/PetroSa2/petrosa_k8s/issues/783)/[`#819`](https://github.com/PetroSa2/petrosa_k8s/issues/819)/[`#881`](https://github.com/PetroSa2/petrosa_k8s/issues/881)/[`#899`](https://github.com/PetroSa2/petrosa_k8s/issues/899)),
which were driven by *unbounded, write-only* collections (`alerts`, `cio_decisions`,
`execution_events` — no TTL, no reader) rather than a bounded, capped-count series like this one.
There is comfortable headroom below the `>80%` Atlas alert threshold tracked in
[`petrosa_k8s#786`](https://github.com/PetroSa2/petrosa_k8s/issues/786) even if `MIN_WARMUP_CANDLES`
or `SUPPORTED_PAIRS`/`SUPPORTED_INTERVALS` grow substantially later.

## AC4 — Downstream consumption

- **`#274` AC3 (Mongo retention/TTL on `candles_*`):** use `MIN_WARMUP_CANDLES = 400` per
  `(pair, timeframe)` as the retention floor. Recommend enforcing it as a **capped-count** job
  (keep newest 400 docs per collection, mirroring the `klines_retention.py` chunked-delete
  pattern but keyed on document rank rather than a calendar cutoff) rather than a pure
  calendar-day TTL index, because a fixed-count cap is what naturally stays bounded regardless of
  gaps in candle cadence; the day-equivalents in the AC2 table above are provided for TTL-index
  implementations that prefer a `Date`-based cutoff instead — both approaches converge on the
  same steady-state count given a regular candle cadence.
  - **Confirmed gap** (`data_manager/maintenance/klines_retention.py:36,178-182`):
    `KLINES_COLLECTION_PREFIX = "klines_"` and `discover_klines_collections()` filters on that
    prefix only — it will **not** discover or prune `candles_{pair}_{period}` collections as
    written. #274 AC3 must either extend this discovery filter or ship an independent job for the
    `candles_*` namespace.
- **`#275` AC1 (warm-up backfill depth):** backfill at least `MIN_WARMUP_CANDLES = 400` most-recent
  candles per `(pair, timeframe)` from the MySQL/historical source into Mongo before the
  primary-store flip's readiness gate (#275 AC2) is allowed to pass.

## AC5 — Change process

This document is the single source of truth for `MIN_WARMUP_CANDLES` and the consumer inventory
above. Update it whenever:

- A new `petrosa-bot-ta-analysis` strategy is added with an indicator lookback **> 400 candles**
  (below 400 is already covered by the existing margin).
- A new service starts consuming candles on the execution path (extend the AC1 inventory table).
- `SUPPORTED_PAIRS` or `SUPPORTED_INTERVALS` change materially (re-run the AC3 footprint math).
- The `minervini_trend_template` fetch-limit bug (see AC1 callout) is fixed in
  `petrosa-bot-ta-analysis` — re-verify 400 still covers its 265-candle requirement with margin
  (it does, at time of writing).

## Related

- [`PetroSa2/petrosa-data-manager#274`](https://github.com/PetroSa2/petrosa-data-manager/issues/274) — primary candle-store flip (Mongo hot path, MySQL fallback); consumes this contract's AC3 retention window.
- [`PetroSa2/petrosa-data-manager#275`](https://github.com/PetroSa2/petrosa-data-manager/issues/275) — cutover safety / warm-up backfill; consumes this contract's AC2 candle-count target.
- [`docs/klines-retention.md`](klines-retention.md) — sibling retention job for `klines_*` (MySQL-tier collections); pattern reference for the capped-count job recommended in AC4.
- [`PetroSa2/petrosa_k8s#783`](https://github.com/PetroSa2/petrosa_k8s/issues/783) — Atlas M0 quota P0 incident epic.
- [`PetroSa2/petrosa_k8s#794`](https://github.com/PetroSa2/petrosa_k8s/issues/794) — candle-backend duplication/audit that surfaced the write/read backend mismatch.
