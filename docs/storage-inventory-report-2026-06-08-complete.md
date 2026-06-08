# Storage Inventory Audit Report

Generated: `2026-06-08T18:29:44.592972Z`

- Mongo backend: OK
- MySQL backend: OK

## 400 MB Attribution (on-disk storageSize bytes)

| Category | Bytes |
| --- | ---: |
| `mongo_candles_bytes` | `0` |
| `mongo_klines_logical_bytes` | `101,060,608` |
| `mongo_klines_buckets_bytes` | `0` |
| `mongo_other_app_bytes` | `76,623,872` |
| `mongo_oplog_bytes` | `14,620,872,704` |
| `mongo_system_dbs_bytes` | `0` |
| `mysql_klines_bytes` | `2,840,903,680` |
| `mysql_other_bytes` | `171,982,848` |

## Duplicated candle timeframes (Mongo + MySQL)

- `15m`
- `1d`
- `1h`
- `30m`
- `5m`

## MongoDB databases

### `binance` (app) — storageSize=65,536 dataSize=0 indexSize=155,648 collections=2

| Collection | TS | storageSize | dataSize | indexSize | count | class |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| `leader_election` |  | 32,768 | 0 | 122,880 | 0 | live |

### `petrosa` (app) — storageSize=520,192 dataSize=569,699 indexSize=991,232 collections=16

| Collection | TS | storageSize | dataSize | indexSize | count | class |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| `app_config` |  | 36,864 | 1,408 | 36,864 | 1 | live |
| `app_config_audit` |  | 36,864 | 1,418 | 73,728 | 1 | live |
| `config_rate_limits` |  | 163,840 | 554,612 | 102,400 | 3,418 | orphan-suspect |
| `daily_pnl` |  | 36,864 | 8,364 | 36,864 | 102 | orphan-suspect |
| `distributed_locks` |  | 32,768 | 0 | 65,536 | 0 | live |
| `leader_election` |  | 32,768 | 0 | 24,576 | 0 | live |
| `leverage_status` |  | 4,096 | 0 | 16,384 | 0 | live |
| `positions` |  | 77,824 | 2,931 | 90,112 | 10 | orphan-suspect |
| `strategy_config_audit` |  | 4,096 | 0 | 12,288 | 0 | live |
| `strategy_configs_global` |  | 4,096 | 0 | 8,192 | 0 | live |
| `strategy_configs_symbol` |  | 4,096 | 0 | 8,192 | 0 | live |
| `strategy_lifecycle_events` |  | 4,096 | 0 | 8,192 | 0 | live |
| `trading_configs_audit` |  | 36,864 | 782 | 294,912 | 2 | live |
| `trading_configs_global` |  | 4,096 | 0 | 12,288 | 0 | live |
| `trading_configs_symbol` |  | 4,096 | 0 | 16,384 | 0 | live |
| `trading_configs_symbol_side` |  | 36,864 | 184 | 184,320 | 1 | live |

### `petrosa_data_manager` (app) — storageSize=177,131,520 dataSize=420,131,173 indexSize=115,277,824 collections=75

| Collection | TS | storageSize | dataSize | indexSize | count | class |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| `alerts` |  | 217,088 | 1,064,164 | 237,568 | 1,500 | live |
| `analytics_ADAUSDT_correlation` |  | 90,112 | 320,207 | 61,440 | 705 | orphan-suspect |
| `analytics_ADAUSDT_deviation` |  | 118,784 | 610,869 | 69,632 | 1,531 | orphan-suspect |
| `analytics_ADAUSDT_regime` |  | 90,112 | 341,850 | 61,440 | 795 | orphan-suspect |
| `analytics_ADAUSDT_seasonality` |  | 94,208 | 407,176 | 53,248 | 616 | orphan-suspect |
| `analytics_ADAUSDT_trend` |  | 77,824 | 267,458 | 61,440 | 773 | orphan-suspect |
| `analytics_ADAUSDT_volatility` |  | 110,592 | 498,704 | 69,632 | 1,539 | orphan-suspect |
| `analytics_ADAUSDT_volume` |  | 86,016 | 235,431 | 61,440 | 777 | orphan-suspect |
| `analytics_BNBUSDT_correlation` |  | 90,112 | 320,207 | 61,440 | 705 | orphan-suspect |
| `analytics_BNBUSDT_deviation` |  | 126,976 | 634,809 | 69,632 | 1,591 | orphan-suspect |
| `analytics_BNBUSDT_regime` |  | 86,016 | 353,030 | 61,440 | 821 | orphan-suspect |
| `analytics_BNBUSDT_seasonality` |  | 94,208 | 426,345 | 53,248 | 645 | orphan-suspect |
| `analytics_BNBUSDT_trend` |  | 77,824 | 277,838 | 61,440 | 803 | orphan-suspect |
| `analytics_BNBUSDT_volatility` |  | 110,592 | 517,152 | 69,632 | 1,596 | orphan-suspect |
| `analytics_BNBUSDT_volume` |  | 86,016 | 243,612 | 61,440 | 804 | orphan-suspect |
| `analytics_BTCUSDT_correlation` |  | 86,016 | 312,204 | 61,440 | 706 | orphan-suspect |
| `analytics_BTCUSDT_deviation` |  | 126,976 | 667,926 | 69,632 | 1,674 | orphan-suspect |
| `analytics_BTCUSDT_regime` |  | 86,016 | 374,530 | 61,440 | 871 | orphan-suspect |
| `analytics_BTCUSDT_seasonality` |  | 114,688 | 452,785 | 53,248 | 685 | orphan-suspect |
| `analytics_BTCUSDT_trend` |  | 77,824 | 294,792 | 61,440 | 852 | orphan-suspect |
| `analytics_BTCUSDT_volatility` |  | 110,592 | 547,312 | 73,728 | 1,689 | orphan-suspect |
| `analytics_BTCUSDT_volume` |  | 90,112 | 258,762 | 61,440 | 854 | orphan-suspect |
| `analytics_ETHUSDT_correlation` |  | 90,112 | 320,207 | 61,440 | 705 | orphan-suspect |
| `analytics_ETHUSDT_deviation` |  | 126,976 | 649,173 | 69,632 | 1,627 | orphan-suspect |
| `analytics_ETHUSDT_regime` |  | 86,016 | 364,210 | 61,440 | 847 | orphan-suspect |
| `analytics_ETHUSDT_seasonality` |  | 94,208 | 438,904 | 53,248 | 664 | orphan-suspect |
| `analytics_ETHUSDT_trend` |  | 77,824 | 284,066 | 61,440 | 821 | orphan-suspect |
| `analytics_ETHUSDT_volatility` |  | 118,784 | 530,128 | 69,632 | 1,636 | orphan-suspect |
| `analytics_ETHUSDT_volume` |  | 94,208 | 250,278 | 61,440 | 826 | orphan-suspect |
| `analytics_LINKUSDT_correlation` |  | 86,016 | 320,459 | 61,440 | 704 | orphan-suspect |
| `analytics_LINKUSDT_deviation` |  | 118,784 | 579,600 | 69,632 | 1,449 | orphan-suspect |
| `analytics_LINKUSDT_regime` |  | 77,824 | 330,379 | 61,440 | 763 | orphan-suspect |
| `analytics_LINKUSDT_seasonality` |  | 94,208 | 379,988 | 53,248 | 574 | orphan-suspect |
| `analytics_LINKUSDT_trend` |  | 77,824 | 252,963 | 61,440 | 729 | orphan-suspect |
| `analytics_LINKUSDT_volatility` |  | 102,400 | 474,877 | 69,632 | 1,461 | orphan-suspect |
| `analytics_LINKUSDT_volume` |  | 86,016 | 222,528 | 61,440 | 732 | orphan-suspect |
| `analytics_LTCUSDT_correlation` |  | 86,016 | 319,755 | 61,440 | 704 | orphan-suspect |
| `analytics_LTCUSDT_deviation` |  | 118,784 | 572,565 | 69,632 | 1,435 | orphan-suspect |
| `analytics_LTCUSDT_regime` |  | 69,632 | 324,220 | 61,440 | 754 | orphan-suspect |
| `analytics_LTCUSDT_seasonality` |  | 86,016 | 374,787 | 53,248 | 567 | orphan-suspect |
| `analytics_LTCUSDT_trend` |  | 77,824 | 249,466 | 61,440 | 721 | orphan-suspect |
| `analytics_LTCUSDT_volatility` |  | 102,400 | 465,608 | 69,632 | 1,437 | orphan-suspect |
| `analytics_LTCUSDT_volume` |  | 86,016 | 218,766 | 61,440 | 722 | orphan-suspect |
| `analytics_SOLUSDT_correlation` |  | 45,056 | 42,679 | 36,864 | 91 | orphan-suspect |
| `analytics_SOLUSDT_deviation` |  | 53,248 | 101,745 | 45,056 | 255 | orphan-suspect |
| `analytics_SOLUSDT_regime` |  | 45,056 | 52,272 | 36,864 | 121 | orphan-suspect |
| `analytics_SOLUSDT_seasonality` |  | 53,248 | 86,591 | 36,864 | 131 | orphan-suspect |
| `analytics_SOLUSDT_trend` |  | 45,056 | 46,018 | 36,864 | 133 | orphan-suspect |
| `analytics_SOLUSDT_volatility` |  | 53,248 | 84,288 | 45,056 | 260 | orphan-suspect |
| `analytics_SOLUSDT_volume` |  | 45,056 | 41,208 | 36,864 | 136 | orphan-suspect |
| `analytics_XRPUSDT_correlation` |  | 94,208 | 319,755 | 61,440 | 704 | orphan-suspect |
| `analytics_XRPUSDT_deviation` |  | 131,072 | 566,181 | 69,632 | 1,419 | orphan-suspect |
| `analytics_XRPUSDT_regime` |  | 86,016 | 321,640 | 61,440 | 748 | orphan-suspect |
| `analytics_XRPUSDT_seasonality` |  | 86,016 | 370,160 | 53,248 | 560 | orphan-suspect |
| `analytics_XRPUSDT_trend` |  | 77,824 | 246,006 | 61,440 | 711 | orphan-suspect |
| `analytics_XRPUSDT_volatility` |  | 102,400 | 460,096 | 69,632 | 1,420 | orphan-suspect |
| `analytics_XRPUSDT_volume` |  | 86,016 | 216,039 | 61,440 | 713 | orphan-suspect |
| `analytics_correlation_matrix` |  | 262,144 | 1,197,151 | 61,440 | 703 | orphan-suspect |
| `app_config` |  | 36,864 | 1,396 | 36,864 | 1 | live |
| `app_config_audit` |  | 36,864 | 1,424 | 36,864 | 1 | live |
| `cio_decisions` |  | 1,875,968 | 5,767,919 | 991,232 | 7,662 | live |
| `execution_events` |  | 1,081,344 | 3,792,729 | 700,416 | 7,611 | live |
| `intents` |  | 67,231,744 | 366,614,447 | 34,263,040 | 397,741 | live |
| `klines_15m` |  | 11,739,136 | 4,736,508 | 4,689,920 | 7,410 | duplicated |
| `klines_1d` |  | 188,416 | 383,476 | 53,248 | 601 | duplicated |
| `klines_1h` |  | 3,518,464 | 4,698,336 | 897,024 | 7,370 | duplicated |
| `klines_30m` |  | 5,746,688 | 4,501,099 | 1,421,312 | 7,060 | duplicated |
| `klines_5m` |  | 79,867,904 | 7,786,104 | 68,186,112 | 12,220 | duplicated |
| `leader_election` |  | 32,768 | 0 | 114,688 | 0 | live |
| `pnl_events` |  | 4,096 | 0 | 12,288 | 0 | live |
| `tickers_BTCUSDT` |  | 45,056 | 23,407 | 36,864 | 89 | live |
| `tickers_ETHUSDT` |  | 45,056 | 23,407 | 36,864 | 89 | live |
| `trades_BTCUSDT` |  | 90,112 | 149,593 | 61,440 | 824 | live |
| `trades_ETHUSDT` |  | 81,920 | 149,160 | 57,344 | 822 | live |
| `trading_configs_audit` |  | 36,864 | 249 | 36,864 | 2 | live |

### `admin` (system) — storageSize=0 dataSize=0 indexSize=0 collections=0

| Collection | TS | storageSize | dataSize | indexSize | count | class |
| --- | --- | ---: | ---: | ---: | ---: | --- |

### `local` (system) — storageSize=0 dataSize=0 indexSize=0 collections=0
_error_: `Failed to fetch dbStats for local: (Unauthorized) not authorized on local to execute command { dbStats: 1, lsid: { id: {4 [204 134 232 255 133 125 79 94 140 4 98 244 66 171 38 86]} }, $clusterTime: { clusterTime: {1780943461 2}, signature: { hash: {0 [112 150 179 181 174 189 18 210 241 248 55 113 191 218 120 195 84 179 162 134]}, keyId: 7584478268355510272.000000 } }, $db: "local" }, full error: {'ok': 0, 'errmsg': '(Unauthorized) not authorized on local to execute command { dbStats: 1, lsid: { id: {4 [204 134 232 255 133 125 79 94 140 4 98 244 66 171 38 86]} }, $clusterTime: { clusterTime: {1780943461 2}, signature: { hash: {0 [112 150 179 181 174 189 18 210 241 248 55 113 191 218 120 195 84 179 162 134]}, keyId: 7584478268355510272.000000 } }, $db: "local" }', 'code': 8000, 'codeName': 'AtlasError'}`

## Oplog (`local.oplog.rs`) — storageSize=14,620,872,704 size=3,395,871,603

## MySQL schemas

### `petrosa_crypto` — base-table bytes=3,012,886,528 (26 tables / 14 views)

| Table | Type | DATA_LENGTH | INDEX_LENGTH | rows~ | class |
| --- | --- | ---: | ---: | ---: | --- |
| `audit_logs` | BASE TABLE | 1,589,248 | 1,900,544 | 4,533 | live |
| `backfill_jobs` | BASE TABLE | 458,752 | 524,288 | 3,034 | live |
| `contribution_summary` | VIEW | 0 | 0 | 0 | view |
| `datasets` | BASE TABLE | 16,384 | 16,384 | 0 | live |
| `exchange_positions` | BASE TABLE | 16,384 | 32,768 | 0 | live |
| `extraction_metadata` | BASE TABLE | 16,384 | 16,384 | 0 | orphan-suspect |
| `funding_rates` | BASE TABLE | 16,384 | 49,152 | 0 | orphan-suspect |
| `health_metrics` | BASE TABLE | 75,251,712 | 68,452,352 | 393,218 | live |
| `klines_12h` | VIEW | 0 | 0 | 0 | view |
| `klines_15m` | VIEW | 0 | 0 | 0 | view |
| `klines_1d` | VIEW | 0 | 0 | 0 | view |
| `klines_1h` | VIEW | 0 | 0 | 0 | view |
| `klines_1m` | VIEW | 0 | 0 | 0 | view |
| `klines_2h` | VIEW | 0 | 0 | 0 | view |
| `klines_30m` | VIEW | 0 | 0 | 0 | view |
| `klines_3m` | VIEW | 0 | 0 | 0 | view |
| `klines_4h` | VIEW | 0 | 0 | 0 | view |
| `klines_5m` | VIEW | 0 | 0 | 0 | view |
| `klines_6h` | VIEW | 0 | 0 | 0 | view |
| `klines_8h` | VIEW | 0 | 0 | 0 | view |
| `klines_d1` | BASE TABLE | 3,686,400 | 4,767,744 | 13,604 | duplicated |
| `klines_h1` | BASE TABLE | 79,478,784 | 55,312,384 | 331,388 | duplicated |
| `klines_h12` | BASE TABLE | 16,384 | 49,152 | 0 | live |
| `klines_h2` | BASE TABLE | 16,384 | 49,152 | 0 | live |
| `klines_h4` | BASE TABLE | 16,384 | 49,152 | 0 | live |
| `klines_h6` | BASE TABLE | 16,384 | 49,152 | 0 | live |
| `klines_h8` | BASE TABLE | 16,384 | 49,152 | 0 | live |
| `klines_m1` | BASE TABLE | 16,384 | 49,152 | 0 | live |
| `klines_m15` | BASE TABLE | 327,155,712 | 250,937,344 | 1,466,256 | duplicated |
| `klines_m3` | BASE TABLE | 16,384 | 49,152 | 0 | live |
| `klines_m30` | BASE TABLE | 177,209,344 | 126,812,160 | 767,121 | duplicated |
| `klines_m5` | BASE TABLE | 1,029,701,632 | 785,383,424 | 4,688,089 | duplicated |
| `lineage_records` | BASE TABLE | 16,384 | 16,384 | 0 | live |
| `positions` | BASE TABLE | 16,384 | 98,304 | 0 | live |
| `position_contributions` | BASE TABLE | 16,384 | 81,920 | 0 | orphan-suspect |
| `schemas` | BASE TABLE | 16,384 | 49,152 | 0 | live |
| `signals` | BASE TABLE | 17,317,888 | 5,816,320 | 44,507 | live |
| `strategy_performance` | VIEW | 0 | 0 | 0 | view |
| `strategy_positions` | BASE TABLE | 16,384 | 98,304 | 0 | live |
| `trades` | BASE TABLE | 16,384 | 49,152 | 0 | orphan-suspect |

```json
{
  "generated_at": "2026-06-08T18:29:44.592972Z",
  "mongo_ok": true,
  "mysql_ok": true,
  "mongo_error": null,
  "mysql_error": null,
  "mongo_databases": [
    {
      "name": "binance",
      "is_system": false,
      "storage_size": 65536,
      "data_size": 0,
      "index_size": 155648,
      "collections": 2,
      "objects": 0,
      "collection_stats": [
        {
          "db_name": "binance",
          "name": "leader_election",
          "is_timeseries": false,
          "storage_size": 32768,
          "data_size": 0,
          "total_index_size": 122880,
          "count": 0,
          "n_indexes": 4,
          "avg_obj_size": 0,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": null,
          "classification": "live",
          "error": null
        }
      ],
      "error": null
    },
    {
      "name": "petrosa",
      "is_system": false,
      "storage_size": 520192,
      "data_size": 569699,
      "index_size": 991232,
      "collections": 16,
      "objects": 3535,
      "collection_stats": [
        {
          "db_name": "petrosa",
          "name": "app_config",
          "is_timeseries": false,
          "storage_size": 36864,
          "data_size": 1408,
          "total_index_size": 36864,
          "count": 1,
          "n_indexes": 1,
          "avg_obj_size": 1408,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T12:14:35.010000",
          "classification": "live",
          "error": null
        },
        {
          "db_name": "petrosa",
          "name": "app_config_audit",
          "is_timeseries": false,
          "storage_size": 36864,
          "data_size": 1418,
          "total_index_size": 73728,
          "count": 1,
          "n_indexes": 2,
          "avg_obj_size": 1418,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T12:14:35+00:00",
          "classification": "live",
          "error": null
        },
        {
          "db_name": "petrosa",
          "name": "config_rate_limits",
          "is_timeseries": false,
          "storage_size": 163840,
          "data_size": 554612,
          "total_index_size": 102400,
          "count": 3418,
          "n_indexes": 1,
          "avg_obj_size": 162,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-04-09T18:28:17.352000",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa",
          "name": "daily_pnl",
          "is_timeseries": false,
          "storage_size": 36864,
          "data_size": 8364,
          "total_index_size": 36864,
          "count": 102,
          "n_indexes": 1,
          "avg_obj_size": 82,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2025-10-24T00:00:00+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa",
          "name": "distributed_locks",
          "is_timeseries": false,
          "storage_size": 32768,
          "data_size": 0,
          "total_index_size": 65536,
          "count": 0,
          "n_indexes": 2,
          "avg_obj_size": 0,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": null,
          "classification": "live",
          "error": null
        },
        {
          "db_name": "petrosa",
          "name": "leader_election",
          "is_timeseries": false,
          "storage_size": 32768,
          "data_size": 0,
          "total_index_size": 24576,
          "count": 0,
          "n_indexes": 1,
          "avg_obj_size": 0,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": null,
          "classification": "live",
          "error": null
        },
        {
          "db_name": "petrosa",
          "name": "leverage_status",
          "is_timeseries": false,
          "storage_size": 4096,
          "data_size": 0,
          "total_index_size": 16384,
          "count": 0,
          "n_indexes": 4,
          "avg_obj_size": 0,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": null,
          "classification": "live",
          "error": null
        },
        {
          "db_name": "petrosa",
          "name": "positions",
          "is_timeseries": false,
          "storage_size": 77824,
          "data_size": 2931,
          "total_index_size": 90112,
          "count": 10,
          "n_indexes": 1,
          "avg_obj_size": 293,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2025-10-21T13:12:10+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa",
          "name": "strategy_config_audit",
          "is_timeseries": false,
          "storage_size": 4096,
          "data_size": 0,
          "total_index_size": 12288,
          "count": 0,
          "n_indexes": 3,
          "avg_obj_size": 0,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": null,
          "classification": "live",
          "error": null
        },
        {
          "db_name": "petrosa",
          "name": "strategy_configs_global",
          "is_timeseries": false,
          "storage_size": 4096,
          "data_size": 0,
          "total_index_size": 8192,
          "count": 0,
          "n_indexes": 2,
          "avg_obj_size": 0,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": null,
          "classification": "live",
          "error": null
        },
        {
          "db_name": "petrosa",
          "name": "strategy_configs_symbol",
          "is_timeseries": false,
          "storage_size": 4096,
          "data_size": 0,
          "total_index_size": 8192,
          "count": 0,
          "n_indexes": 2,
          "avg_obj_size": 0,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": null,
          "classification": "live",
          "error": null
        },
        {
          "db_name": "petrosa",
          "name": "strategy_lifecycle_events",
          "is_timeseries": false,
          "storage_size": 4096,
          "data_size": 0,
          "total_index_size": 8192,
          "count": 0,
          "n_indexes": 2,
          "avg_obj_size": 0,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": null,
          "classification": "live",
          "error": null
        },
        {
          "db_name": "petrosa",
          "name": "trading_configs_audit",
          "is_timeseries": false,
          "storage_size": 36864,
          "data_size": 782,
          "total_index_size": 294912,
          "count": 2,
          "n_indexes": 8,
          "avg_obj_size": 391,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2025-10-20T17:41:18.250000",
          "classification": "live",
          "error": null
        },
        {
          "db_name": "petrosa",
          "name": "trading_configs_global",
          "is_timeseries": false,
          "storage_size": 4096,
          "data_size": 0,
          "total_index_size": 12288,
          "count": 0,
          "n_indexes": 3,
          "avg_obj_size": 0,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": null,
          "classification": "live",
          "error": null
        },
        {
          "db_name": "petrosa",
          "name": "trading_configs_symbol",
          "is_timeseries": false,
          "storage_size": 4096,
          "data_size": 0,
          "total_index_size": 16384,
          "count": 0,
          "n_indexes": 4,
          "avg_obj_size": 0,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": null,
          "classification": "live",
          "error": null
        },
        {
          "db_name": "petrosa",
          "name": "trading_configs_symbol_side",
          "is_timeseries": false,
          "storage_size": 36864,
          "data_size": 184,
          "total_index_size": 184320,
          "count": 1,
          "n_indexes": 5,
          "avg_obj_size": 184,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2025-10-20T17:40:51.020000",
          "classification": "live",
          "error": null
        }
      ],
      "error": null
    },
    {
      "name": "petrosa_data_manager",
      "is_system": false,
      "storage_size": 177131520,
      "data_size": 420131173,
      "index_size": 115277824,
      "collections": 75,
      "objects": 500018,
      "collection_stats": [
        {
          "db_name": "petrosa_data_manager",
          "name": "alerts",
          "is_timeseries": false,
          "storage_size": 217088,
          "data_size": 1064164,
          "total_index_size": 237568,
          "count": 1500,
          "n_indexes": 3,
          "avg_obj_size": 709,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": null,
          "classification": "live",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_ADAUSDT_correlation",
          "is_timeseries": false,
          "storage_size": 90112,
          "data_size": 320207,
          "total_index_size": 61440,
          "count": 705,
          "n_indexes": 1,
          "avg_obj_size": 454,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T16:49:02+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_ADAUSDT_deviation",
          "is_timeseries": false,
          "storage_size": 118784,
          "data_size": 610869,
          "total_index_size": 69632,
          "count": 1531,
          "n_indexes": 1,
          "avg_obj_size": 399,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:06:50+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_ADAUSDT_regime",
          "is_timeseries": false,
          "storage_size": 90112,
          "data_size": 341850,
          "total_index_size": 61440,
          "count": 795,
          "n_indexes": 1,
          "avg_obj_size": 430,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:06:51+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_ADAUSDT_seasonality",
          "is_timeseries": false,
          "storage_size": 94208,
          "data_size": 407176,
          "total_index_size": 53248,
          "count": 616,
          "n_indexes": 1,
          "avg_obj_size": 661,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:06:48+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_ADAUSDT_trend",
          "is_timeseries": false,
          "storage_size": 77824,
          "data_size": 267458,
          "total_index_size": 61440,
          "count": 773,
          "n_indexes": 1,
          "avg_obj_size": 346,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:06:45+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_ADAUSDT_volatility",
          "is_timeseries": false,
          "storage_size": 110592,
          "data_size": 498704,
          "total_index_size": 69632,
          "count": 1539,
          "n_indexes": 1,
          "avg_obj_size": 324,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:06:49+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_ADAUSDT_volume",
          "is_timeseries": false,
          "storage_size": 86016,
          "data_size": 235431,
          "total_index_size": 61440,
          "count": 777,
          "n_indexes": 1,
          "avg_obj_size": 303,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:06:43+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_BNBUSDT_correlation",
          "is_timeseries": false,
          "storage_size": 90112,
          "data_size": 320207,
          "total_index_size": 61440,
          "count": 705,
          "n_indexes": 1,
          "avg_obj_size": 454,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T16:49:02+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_BNBUSDT_deviation",
          "is_timeseries": false,
          "storage_size": 126976,
          "data_size": 634809,
          "total_index_size": 69632,
          "count": 1591,
          "n_indexes": 1,
          "avg_obj_size": 399,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:12:02+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_BNBUSDT_regime",
          "is_timeseries": false,
          "storage_size": 86016,
          "data_size": 353030,
          "total_index_size": 61440,
          "count": 821,
          "n_indexes": 1,
          "avg_obj_size": 430,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:06:42+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_BNBUSDT_seasonality",
          "is_timeseries": false,
          "storage_size": 94208,
          "data_size": 426345,
          "total_index_size": 53248,
          "count": 645,
          "n_indexes": 1,
          "avg_obj_size": 661,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:11:57+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_BNBUSDT_trend",
          "is_timeseries": false,
          "storage_size": 77824,
          "data_size": 277838,
          "total_index_size": 61440,
          "count": 803,
          "n_indexes": 1,
          "avg_obj_size": 346,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:11:51+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_BNBUSDT_volatility",
          "is_timeseries": false,
          "storage_size": 110592,
          "data_size": 517152,
          "total_index_size": 69632,
          "count": 1596,
          "n_indexes": 1,
          "avg_obj_size": 324,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:11:58+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_BNBUSDT_volume",
          "is_timeseries": false,
          "storage_size": 86016,
          "data_size": 243612,
          "total_index_size": 61440,
          "count": 804,
          "n_indexes": 1,
          "avg_obj_size": 303,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:11:49+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_BTCUSDT_correlation",
          "is_timeseries": false,
          "storage_size": 86016,
          "data_size": 312204,
          "total_index_size": 61440,
          "count": 706,
          "n_indexes": 1,
          "avg_obj_size": 442,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T16:49:01+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_BTCUSDT_deviation",
          "is_timeseries": false,
          "storage_size": 126976,
          "data_size": 667926,
          "total_index_size": 69632,
          "count": 1674,
          "n_indexes": 1,
          "avg_obj_size": 399,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:11:28+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_BTCUSDT_regime",
          "is_timeseries": false,
          "storage_size": 86016,
          "data_size": 374530,
          "total_index_size": 61440,
          "count": 871,
          "n_indexes": 1,
          "avg_obj_size": 430,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:11:29+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_BTCUSDT_seasonality",
          "is_timeseries": false,
          "storage_size": 114688,
          "data_size": 452785,
          "total_index_size": 53248,
          "count": 685,
          "n_indexes": 1,
          "avg_obj_size": 661,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:11:22+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_BTCUSDT_trend",
          "is_timeseries": false,
          "storage_size": 77824,
          "data_size": 294792,
          "total_index_size": 61440,
          "count": 852,
          "n_indexes": 1,
          "avg_obj_size": 346,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:11:16+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_BTCUSDT_volatility",
          "is_timeseries": false,
          "storage_size": 110592,
          "data_size": 547312,
          "total_index_size": 73728,
          "count": 1689,
          "n_indexes": 1,
          "avg_obj_size": 324,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:11:25+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_BTCUSDT_volume",
          "is_timeseries": false,
          "storage_size": 90112,
          "data_size": 258762,
          "total_index_size": 61440,
          "count": 854,
          "n_indexes": 1,
          "avg_obj_size": 303,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:11:14+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_ETHUSDT_correlation",
          "is_timeseries": false,
          "storage_size": 90112,
          "data_size": 320207,
          "total_index_size": 61440,
          "count": 705,
          "n_indexes": 1,
          "avg_obj_size": 454,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T16:49:01+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_ETHUSDT_deviation",
          "is_timeseries": false,
          "storage_size": 126976,
          "data_size": 649173,
          "total_index_size": 69632,
          "count": 1627,
          "n_indexes": 1,
          "avg_obj_size": 399,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:11:45+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_ETHUSDT_regime",
          "is_timeseries": false,
          "storage_size": 86016,
          "data_size": 364210,
          "total_index_size": 61440,
          "count": 847,
          "n_indexes": 1,
          "avg_obj_size": 430,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:11:46+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_ETHUSDT_seasonality",
          "is_timeseries": false,
          "storage_size": 94208,
          "data_size": 438904,
          "total_index_size": 53248,
          "count": 664,
          "n_indexes": 1,
          "avg_obj_size": 661,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:11:41+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_ETHUSDT_trend",
          "is_timeseries": false,
          "storage_size": 77824,
          "data_size": 284066,
          "total_index_size": 61440,
          "count": 821,
          "n_indexes": 1,
          "avg_obj_size": 346,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:11:34+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_ETHUSDT_volatility",
          "is_timeseries": false,
          "storage_size": 118784,
          "data_size": 530128,
          "total_index_size": 69632,
          "count": 1636,
          "n_indexes": 1,
          "avg_obj_size": 324,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:11:42+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_ETHUSDT_volume",
          "is_timeseries": false,
          "storage_size": 94208,
          "data_size": 250278,
          "total_index_size": 61440,
          "count": 826,
          "n_indexes": 1,
          "avg_obj_size": 303,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:11:32+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_LINKUSDT_correlation",
          "is_timeseries": false,
          "storage_size": 86016,
          "data_size": 320459,
          "total_index_size": 61440,
          "count": 704,
          "n_indexes": 1,
          "avg_obj_size": 455,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T16:49:02+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_LINKUSDT_deviation",
          "is_timeseries": false,
          "storage_size": 118784,
          "data_size": 579600,
          "total_index_size": 69632,
          "count": 1449,
          "n_indexes": 1,
          "avg_obj_size": 400,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:08:45+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_LINKUSDT_regime",
          "is_timeseries": false,
          "storage_size": 77824,
          "data_size": 330379,
          "total_index_size": 61440,
          "count": 763,
          "n_indexes": 1,
          "avg_obj_size": 433,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:08:46+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_LINKUSDT_seasonality",
          "is_timeseries": false,
          "storage_size": 94208,
          "data_size": 379988,
          "total_index_size": 53248,
          "count": 574,
          "n_indexes": 1,
          "avg_obj_size": 662,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:07:55+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_LINKUSDT_trend",
          "is_timeseries": false,
          "storage_size": 77824,
          "data_size": 252963,
          "total_index_size": 61440,
          "count": 729,
          "n_indexes": 1,
          "avg_obj_size": 347,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:07:03+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_LINKUSDT_volatility",
          "is_timeseries": false,
          "storage_size": 102400,
          "data_size": 474877,
          "total_index_size": 69632,
          "count": 1461,
          "n_indexes": 1,
          "avg_obj_size": 325,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:08:44+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_LINKUSDT_volume",
          "is_timeseries": false,
          "storage_size": 86016,
          "data_size": 222528,
          "total_index_size": 61440,
          "count": 732,
          "n_indexes": 1,
          "avg_obj_size": 304,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:07:02+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_LTCUSDT_correlation",
          "is_timeseries": false,
          "storage_size": 86016,
          "data_size": 319755,
          "total_index_size": 61440,
          "count": 704,
          "n_indexes": 1,
          "avg_obj_size": 454,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T16:49:02+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_LTCUSDT_deviation",
          "is_timeseries": false,
          "storage_size": 118784,
          "data_size": 572565,
          "total_index_size": 69632,
          "count": 1435,
          "n_indexes": 1,
          "avg_obj_size": 399,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:08:55+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_LTCUSDT_regime",
          "is_timeseries": false,
          "storage_size": 69632,
          "data_size": 324220,
          "total_index_size": 61440,
          "count": 754,
          "n_indexes": 1,
          "avg_obj_size": 430,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T16:48:44+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_LTCUSDT_seasonality",
          "is_timeseries": false,
          "storage_size": 86016,
          "data_size": 374787,
          "total_index_size": 53248,
          "count": 567,
          "n_indexes": 1,
          "avg_obj_size": 661,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:08:52+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_LTCUSDT_trend",
          "is_timeseries": false,
          "storage_size": 77824,
          "data_size": 249466,
          "total_index_size": 61440,
          "count": 721,
          "n_indexes": 1,
          "avg_obj_size": 346,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:08:49+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_LTCUSDT_volatility",
          "is_timeseries": false,
          "storage_size": 102400,
          "data_size": 465608,
          "total_index_size": 69632,
          "count": 1437,
          "n_indexes": 1,
          "avg_obj_size": 324,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:08:53+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_LTCUSDT_volume",
          "is_timeseries": false,
          "storage_size": 86016,
          "data_size": 218766,
          "total_index_size": 61440,
          "count": 722,
          "n_indexes": 1,
          "avg_obj_size": 303,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:08:48+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_SOLUSDT_correlation",
          "is_timeseries": false,
          "storage_size": 45056,
          "data_size": 42679,
          "total_index_size": 36864,
          "count": 91,
          "n_indexes": 1,
          "avg_obj_size": 469,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T16:49:02+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_SOLUSDT_deviation",
          "is_timeseries": false,
          "storage_size": 53248,
          "data_size": 101745,
          "total_index_size": 45056,
          "count": 255,
          "n_indexes": 1,
          "avg_obj_size": 399,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:07:00+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_SOLUSDT_regime",
          "is_timeseries": false,
          "storage_size": 45056,
          "data_size": 52272,
          "total_index_size": 36864,
          "count": 121,
          "n_indexes": 1,
          "avg_obj_size": 432,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:07:00+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_SOLUSDT_seasonality",
          "is_timeseries": false,
          "storage_size": 53248,
          "data_size": 86591,
          "total_index_size": 36864,
          "count": 131,
          "n_indexes": 1,
          "avg_obj_size": 661,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:06:57+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_SOLUSDT_trend",
          "is_timeseries": false,
          "storage_size": 45056,
          "data_size": 46018,
          "total_index_size": 36864,
          "count": 133,
          "n_indexes": 1,
          "avg_obj_size": 346,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:06:54+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_SOLUSDT_volatility",
          "is_timeseries": false,
          "storage_size": 53248,
          "data_size": 84288,
          "total_index_size": 45056,
          "count": 260,
          "n_indexes": 1,
          "avg_obj_size": 324,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:06:58+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_SOLUSDT_volume",
          "is_timeseries": false,
          "storage_size": 45056,
          "data_size": 41208,
          "total_index_size": 36864,
          "count": 136,
          "n_indexes": 1,
          "avg_obj_size": 303,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T18:06:53+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_XRPUSDT_correlation",
          "is_timeseries": false,
          "storage_size": 94208,
          "data_size": 319755,
          "total_index_size": 61440,
          "count": 704,
          "n_indexes": 1,
          "avg_obj_size": 454,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T16:49:02+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_XRPUSDT_deviation",
          "is_timeseries": false,
          "storage_size": 131072,
          "data_size": 566181,
          "total_index_size": 69632,
          "count": 1419,
          "n_indexes": 1,
          "avg_obj_size": 399,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T16:48:52+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_XRPUSDT_regime",
          "is_timeseries": false,
          "storage_size": 86016,
          "data_size": 321640,
          "total_index_size": 61440,
          "count": 748,
          "n_indexes": 1,
          "avg_obj_size": 430,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T16:48:53+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_XRPUSDT_seasonality",
          "is_timeseries": false,
          "storage_size": 86016,
          "data_size": 370160,
          "total_index_size": 53248,
          "count": 560,
          "n_indexes": 1,
          "avg_obj_size": 661,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T16:48:50+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_XRPUSDT_trend",
          "is_timeseries": false,
          "storage_size": 77824,
          "data_size": 246006,
          "total_index_size": 61440,
          "count": 711,
          "n_indexes": 1,
          "avg_obj_size": 346,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T16:48:47+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_XRPUSDT_volatility",
          "is_timeseries": false,
          "storage_size": 102400,
          "data_size": 460096,
          "total_index_size": 69632,
          "count": 1420,
          "n_indexes": 1,
          "avg_obj_size": 324,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T16:48:51+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_XRPUSDT_volume",
          "is_timeseries": false,
          "storage_size": 86016,
          "data_size": 216039,
          "total_index_size": 61440,
          "count": 713,
          "n_indexes": 1,
          "avg_obj_size": 303,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T16:48:45+00:00",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "analytics_correlation_matrix",
          "is_timeseries": false,
          "storage_size": 262144,
          "data_size": 1197151,
          "total_index_size": 61440,
          "count": 703,
          "n_indexes": 1,
          "avg_obj_size": 1702,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-13T16:49:02.932000",
          "classification": "orphan-suspect",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "app_config",
          "is_timeseries": false,
          "storage_size": 36864,
          "data_size": 1396,
          "total_index_size": 36864,
          "count": 1,
          "n_indexes": 1,
          "avg_obj_size": 1396,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-03-11T17:54:20.179000",
          "classification": "live",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "app_config_audit",
          "is_timeseries": false,
          "storage_size": 36864,
          "data_size": 1424,
          "total_index_size": 36864,
          "count": 1,
          "n_indexes": 1,
          "avg_obj_size": 1424,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": null,
          "classification": "live",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "cio_decisions",
          "is_timeseries": false,
          "storage_size": 1875968,
          "data_size": 5767919,
          "total_index_size": 991232,
          "count": 7662,
          "n_indexes": 3,
          "avg_obj_size": 752,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-06-05T10:38:19.300000",
          "classification": "live",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "execution_events",
          "is_timeseries": false,
          "storage_size": 1081344,
          "data_size": 3792729,
          "total_index_size": 700416,
          "count": 7611,
          "n_indexes": 3,
          "avg_obj_size": 498,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-06-05T10:38:22.764000",
          "classification": "live",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "intents",
          "is_timeseries": false,
          "storage_size": 67231744,
          "data_size": 366614447,
          "total_index_size": 34263040,
          "count": 397741,
          "n_indexes": 3,
          "avg_obj_size": 921,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-06-06T19:56:49.877000",
          "classification": "live",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "klines_15m",
          "is_timeseries": false,
          "storage_size": 11739136,
          "data_size": 4736508,
          "total_index_size": 4689920,
          "count": 7410,
          "n_indexes": 2,
          "avg_obj_size": 639,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": null,
          "classification": "duplicated",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "klines_1d",
          "is_timeseries": false,
          "storage_size": 188416,
          "data_size": 383476,
          "total_index_size": 53248,
          "count": 601,
          "n_indexes": 1,
          "avg_obj_size": 638,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": null,
          "classification": "duplicated",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "klines_1h",
          "is_timeseries": false,
          "storage_size": 3518464,
          "data_size": 4698336,
          "total_index_size": 897024,
          "count": 7370,
          "n_indexes": 1,
          "avg_obj_size": 637,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": null,
          "classification": "duplicated",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "klines_30m",
          "is_timeseries": false,
          "storage_size": 5746688,
          "data_size": 4501099,
          "total_index_size": 1421312,
          "count": 7060,
          "n_indexes": 2,
          "avg_obj_size": 637,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": null,
          "classification": "duplicated",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "klines_5m",
          "is_timeseries": false,
          "storage_size": 79867904,
          "data_size": 7786104,
          "total_index_size": 68186112,
          "count": 12220,
          "n_indexes": 2,
          "avg_obj_size": 637,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": null,
          "classification": "duplicated",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "leader_election",
          "is_timeseries": false,
          "storage_size": 32768,
          "data_size": 0,
          "total_index_size": 114688,
          "count": 0,
          "n_indexes": 4,
          "avg_obj_size": 0,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": null,
          "classification": "live",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "pnl_events",
          "is_timeseries": false,
          "storage_size": 4096,
          "data_size": 0,
          "total_index_size": 12288,
          "count": 0,
          "n_indexes": 3,
          "avg_obj_size": 0,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": null,
          "classification": "live",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "tickers_BTCUSDT",
          "is_timeseries": false,
          "storage_size": 45056,
          "data_size": 23407,
          "total_index_size": 36864,
          "count": 89,
          "n_indexes": 1,
          "avg_obj_size": 263,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2025-10-21T14:37:05.026000",
          "classification": "live",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "tickers_ETHUSDT",
          "is_timeseries": false,
          "storage_size": 45056,
          "data_size": 23407,
          "total_index_size": 36864,
          "count": 89,
          "n_indexes": 1,
          "avg_obj_size": 263,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2025-10-21T14:37:05.040000",
          "classification": "live",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "trades_BTCUSDT",
          "is_timeseries": false,
          "storage_size": 90112,
          "data_size": 149593,
          "total_index_size": 61440,
          "count": 824,
          "n_indexes": 1,
          "avg_obj_size": 181,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2025-10-21T14:37:05.316000",
          "classification": "live",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "trades_ETHUSDT",
          "is_timeseries": false,
          "storage_size": 81920,
          "data_size": 149160,
          "total_index_size": 57344,
          "count": 822,
          "n_indexes": 1,
          "avg_obj_size": 181,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2025-10-21T14:37:05.155000",
          "classification": "live",
          "error": null
        },
        {
          "db_name": "petrosa_data_manager",
          "name": "trading_configs_audit",
          "is_timeseries": false,
          "storage_size": 36864,
          "data_size": 249,
          "total_index_size": 36864,
          "count": 2,
          "n_indexes": 1,
          "avg_obj_size": 124,
          "backing_bucket_storage_size": null,
          "backing_bucket_data_size": null,
          "newest_doc_age": "2026-06-04T15:11:43.919000",
          "classification": "live",
          "error": null
        }
      ],
      "error": null
    },
    {
      "name": "admin",
      "is_system": true,
      "storage_size": 0,
      "data_size": 0,
      "index_size": 0,
      "collections": 0,
      "objects": 0,
      "collection_stats": [],
      "error": null
    },
    {
      "name": "local",
      "is_system": true,
      "storage_size": 0,
      "data_size": 0,
      "index_size": 0,
      "collections": 0,
      "objects": 0,
      "collection_stats": [],
      "error": "Failed to fetch dbStats for local: (Unauthorized) not authorized on local to execute command { dbStats: 1, lsid: { id: {4 [204 134 232 255 133 125 79 94 140 4 98 244 66 171 38 86]} }, $clusterTime: { clusterTime: {1780943461 2}, signature: { hash: {0 [112 150 179 181 174 189 18 210 241 248 55 113 191 218 120 195 84 179 162 134]}, keyId: 7584478268355510272.000000 } }, $db: \"local\" }, full error: {'ok': 0, 'errmsg': '(Unauthorized) not authorized on local to execute command { dbStats: 1, lsid: { id: {4 [204 134 232 255 133 125 79 94 140 4 98 244 66 171 38 86]} }, $clusterTime: { clusterTime: {1780943461 2}, signature: { hash: {0 [112 150 179 181 174 189 18 210 241 248 55 113 191 218 120 195 84 179 162 134]}, keyId: 7584478268355510272.000000 } }, $db: \"local\" }', 'code': 8000, 'codeName': 'AtlasError'}"
    }
  ],
  "mongo_oplog": {
    "size": 3395871603,
    "count": 3480809,
    "avgObjSize": 975,
    "numOrphanDocs": 0,
    "storageSize": 14620872704,
    "freeStorageSize": 13296807936,
    "capped": true,
    "max": 0,
    "maxSize": 3416653414,
    "wiredTiger": {
      "metadata": {
        "formatVersion": 1,
        "oplogKeyExtractionVersion": 1
      },
      "creationString": "access_pattern_hint=none,allocation_size=4KB,app_metadata=(formatVersion=1,oplogKeyExtractionVersion=1),assert=(commit_timestamp=none,durable_timestamp=none,read_timestamp=none),block_allocation=best,block_compressor=snappy,cache_resident=false,checksum=on,colgroups=,collator=,columns=,dictionary=0,encryption=(keyid=,name=),exclusive=false,extractor=,format=btree,huffman_key=,huffman_value=,ignore_in_memory_cache_size=false,immutable=false,import=(compare_timestamp=oldest_timestamp,enabled=false,file_metadata=,metadata_file=,panic_corrupt=true,repair=false),internal_item_max=0,internal_key_max=0,internal_key_truncate=true,internal_page_max=4KB,key_format=q,key_gap=10,leaf_item_max=0,leaf_key_max=0,leaf_page_max=32KB,leaf_value_max=64MB,log=(enabled=true),lsm=(auto_throttle=true,bloom=true,bloom_bit_count=16,bloom_config=,bloom_hash_count=8,bloom_oldest=false,chunk_count_limit=0,chunk_max=5GB,chunk_size=10MB,merge_custom=(prefix=,start_generation=0,suffix=),merge_max=15,merge_min=0),memory_page_image_max=0,memory_page_max=10m,os_cache_dirty_max=0,os_cache_max=0,prefix_compression=false,prefix_compression_min=4,source=,split_deepen_min_child=0,split_deepen_per_child=0,split_pct=90,tiered_storage=(auth_token=,bucket=,bucket_prefix=,cache_directory=,local_retention=300,name=,object_target_size=0,shared=false),type=file,value_format=u,verbose=[],write_timestamp_usage=none",
      "type": "file",
      "uri": "statistics:table:collection-18--5206743024052771203",
      "LSM": {
        "bloom filter false positives": 0,
        "bloom filter hits": 0,
        "bloom filter misses": 0,
        "bloom filter pages evicted from cache": 0,
        "bloom filter pages read into cache": 0,
        "bloom filters in the LSM tree": 0,
        "chunks in the LSM tree": 0,
        "highest merge generation in the LSM tree": 0,
        "queries that could have benefited from a Bloom filter that did not exist": 0,
        "sleep for LSM checkpoint throttle": 0,
        "sleep for LSM merge throttle": 0,
        "total size of bloom filters": 0
      },
      "autocommit": {
        "retries for readonly operations": 0,
        "retries for update operations": 0
      },
      "backup": {
        "total modified incremental blocks with compressed data": 0,
        "total modified incremental blocks without compressed data": 0
      },
      "block-manager": {
        "allocations requiring file extension": 0,
        "blocks allocated": 405308,
        "blocks freed": 383766,
        "checkpoint size": 1323991040,
        "file allocation unit size": 4096,
        "file bytes available for reuse": 13296807936,
        "file magic number": 120897,
        "file major version number": 1,
        "file size in bytes": 14620872704,
        "minor version number": 0
      },
      "btree": {
        "btree checkpoint generation": 5740,
        "btree clean tree checkpoint expiration time": 0,
        "btree compact pages reviewed": 0,
        "btree compact pages rewritten": 0,
        "btree compact pages skipped": 0,
        "btree expected number of compact bytes rewritten": 0,
        "btree expected number of compact pages rewritten": 0,
        "btree number of pages reconciled during checkpoint": 25494,
        "btree skipped by compaction as process would not reduce size": 0,
        "column-store fixed-size leaf pages": 0,
        "column-store fixed-size time windows": 0,
        "column-store internal pages": 0,
        "column-store variable-size RLE encoded values": 0,
        "column-store variable-size deleted values": 0,
        "column-store variable-size leaf pages": 0,
        "fixed-record size": 0,
        "maximum internal page size": 4096,
        "maximum leaf page key size": 2867,
        "maximum leaf page size": 32768,
        "maximum leaf page value size": 67108864,
        "maximum tree depth": 5,
        "number of key/value pairs": 0,
        "overflow pages": 0,
        "row-store empty values": 0,
        "row-store internal pages": 0,
        "row-store leaf pages": 0
      },
      "cache": {
        "application threads eviction requested with cache fill ratio < 25%": 0,
        "application threads eviction requested with cache fill ratio >= 25% and < 50%": 0,
        "application threads eviction requested with cache fill ratio >= 50% and < 75%": 0,
        "application threads eviction requested with cache fill ratio >= 75%": 0,
        "bytes currently in the cache": 999047428,
        "bytes dirty in the cache cumulative": 21829378743,
        "bytes read into cache": 21704071999,
        "bytes written from cache": 21645807182,
        "checkpoint blocked page eviction": 7,
        "checkpoint of history store file blocked non-history store page eviction": 0,
        "data source pages selected for eviction unable to be evicted": 954,
        "eviction gave up due to detecting a disk value without a timestamp behind the last update on the chain": 0,
        "eviction gave up due to detecting a tombstone without a timestamp ahead of the selected on disk update": 0,
        "eviction gave up due to detecting a tombstone without a timestamp ahead of the selected on disk update after validating the update chain": 0,
        "eviction gave up due to detecting update chain entries without timestamps after the selected on disk update": 0,
        "eviction gave up due to needing to remove a record from the history store but checkpoint is running": 0,
        "eviction gave up due to no progress being made": 0,
        "eviction walk pages queued that had updates": 0,
        "eviction walk pages queued that were clean": 0,
        "eviction walk pages queued that were dirty": 0,
        "eviction walk pages seen that had updates": 16757,
        "eviction walk pages seen that were clean": 5737950,
        "eviction walk pages seen that were dirty": 1715,
        "eviction walk passes of a file": 4074,
        "eviction walk target pages histogram - 0-9": 418,
        "eviction walk target pages histogram - 10-31": 532,
        "eviction walk target pages histogram - 128 and higher": 0,
        "eviction walk target pages histogram - 32-63": 709,
        "eviction walk target pages histogram - 64-128": 2415,
        "eviction walk target pages reduced due to history store cache pressure": 0,
        "eviction walks abandoned": 334,
        "eviction walks gave up because they restarted their walk twice": 58,
        "eviction walks gave up because they saw too many pages and found no candidates": 726,
        "eviction walks gave up because they saw too many pages and found too few candidates": 380,
        "eviction walks random search fails to locate a page, results in a null position": 0,
        "eviction walks reached end of tree": 963,
        "eviction walks restarted": 0,
        "eviction walks started from root of tree": 1498,
        "eviction walks started from saved location in tree": 2576,
        "hazard pointer blocked page eviction": 667,
        "history store table insert calls": 0,
        "history store table insert calls that returned restart": 0,
        "history store table reads": 0,
        "history store table reads missed": 0,
        "history store table reads requiring squashed modifies": 0,
        "history store table resolved updates without timestamps that lose their durable timestamp": 0,
        "history store table truncation by rollback to stable to remove an unstable update": 0,
        "history store table truncation by rollback to stable to remove an update": 0,
        "history store table truncation to remove all the keys of a btree": 0,
        "history store table truncation to remove an update": 0,
        "history store table truncation to remove range of updates due to an update without a timestamp on data page": 0,
        "history store table truncation to remove range of updates due to key being removed from the data page during reconciliation": 0,
        "history store table truncations that would have happened in non-dryrun mode": 0,
        "history store table truncations to remove an unstable update that would have happened in non-dryrun mode": 0,
        "history store table truncations to remove an update that would have happened in non-dryrun mode": 0,
        "history store table updates without timestamps fixed up by reinserting with the fixed timestamp": 0,
        "history store table writes requiring squashed modifies": 0,
        "in-memory page passed criteria to be split": 1240,
        "in-memory page splits": 257,
        "internal page split blocked its eviction": 0,
        "internal pages evicted": 896,
        "internal pages split during eviction": 2,
        "leaf pages split during eviction": 416,
        "locate a random in-mem ref by examining all entries on the root page": 0,
        "modified pages evicted": 1210,
        "multi-block reconciliation blocked whilst checkpoint is running": 0,
        "number of times dirty trigger was reached": 0,
        "number of times eviction trigger was reached": 0,
        "number of times updates trigger was reached": 0,
        "obsolete updates removed": 0,
        "overflow keys on a multiblock row-store page blocked its eviction": 0,
        "overflow pages read into cache": 0,
        "page split during eviction deepened the tree": 0,
        "page written requiring history store records": 0,
        "pages dirtied due to obsolete time window by eviction": 0,
        "pages read into cache": 193107,
        "pages read into cache after truncate": 0,
        "pages read into cache after truncate in prepare state": 0,
        "pages read into cache by checkpoint": 0,
        "pages requested from the cache": 295155535,
        "pages requested from the cache due to pre-fetch": 0,
        "pages requested from the history store": 0,
        "pages seen by eviction walk": 5740273,
        "pages written from cache": 393832,
        "pages written requiring in-memory restoration": 318,
        "recent modification of a page blocked its eviction": 0,
        "reverse splits performed": 47,
        "reverse splits skipped because of VLCS namespace gap restrictions": 0,
        "the number of saved update lists processed in __wt_hs_insert_updates": 0,
        "the number of times full update inserted to history store": 0,
        "the number of times reverse modify inserted to history store": 0,
        "the number of updates processed in __wt_hs_insert_updates": 0,
        "tracked dirty bytes in the cache": 4052799,
        "tracked dirty internal page bytes in the cache": 210110,
        "tracked dirty leaf page bytes in the cache": 3842688,
        "uncommitted truncate blocked page eviction": 0,
        "unmodified pages evicted": 194499
      },
      "cache_walk": {
        "Average difference between current eviction generation when the page was last considered": 0,
        "Average on-disk page image size seen": 0,
        "Average time in cache for pages that have been visited by the eviction server": 0,
        "Average time in cache for pages that have not been visited by the eviction server": 0,
        "Clean pages currently in cache": 0,
        "Current eviction generation": 0,
        "Dirty pages currently in cache": 0,
        "Entries in the root page": 0,
        "Internal pages currently in cache": 0,
        "Leaf pages currently in cache": 0,
        "Maximum difference between current eviction generation when the page was last considered": 0,
        "Maximum page size seen": 0,
        "Minimum on-disk page image size seen": 0,
        "Number of pages never visited by eviction server": 0,
        "On-disk page image sizes smaller than a single allocation unit": 0,
        "Pages created in memory and never written": 0,
        "Pages currently queued for eviction": 0,
        "Pages that could not be queued for eviction": 0,
        "Refs skipped during cache traversal": 0,
        "Size of the root page": 0,
        "Total number of pages currently in cache": 0
      },
      "checkpoint": {
        "checkpoint has acquired a snapshot for its transaction": 0,
        "transaction checkpoints due to obsolete pages": 0
      },
      "checkpoint-cleanup": {
        "pages added for eviction": 28,
        "pages dirtied due to obsolete time window": 13,
        "pages read into cache (reclaim_space)": 0,
        "pages read into cache due to obsolete time window": 0,
        "pages removed": 58676,
        "pages skipped during tree walk": 20570015,
        "pages visited": 30835032
      },
      "compression": {
        "compressed page maximum internal page size prior to compression": 4096,
        "compressed page maximum leaf page size prior to compression ": 131072,
        "page written to disk failed to compress": 688,
        "page written to disk was too small to compress": 209598,
        "pages read from disk": 188715,
        "pages read from disk with compression ratio greater than 64": 0,
        "pages read from disk with compression ratio smaller than  2": 25269,
        "pages read from disk with compression ratio smaller than  4": 46871,
        "pages read from disk with compression ratio smaller than  8": 85981,
        "pages read from disk with compression ratio smaller than 16": 30588,
        "pages read from disk with compression ratio smaller than 32": 6,
        "pages read from disk with compression ratio smaller than 64": 0,
        "pages written to disk": 183546,
        "pages written to disk with compression ratio greater than 64": 0,
        "pages written to disk with compression ratio smaller than  2": 10276,
        "pages written to disk with compression ratio smaller than  4": 24993,
        "pages written to disk with compression ratio smaller than  8": 143797,
        "pages written to disk with compression ratio smaller than 16": 4480,
        "pages written to disk with compression ratio smaller than 32": 0,
        "pages written to disk with compression ratio smaller than 64": 0
      },
      "cursor": {
        "Total number of deleted pages skipped during tree walk": 64,
        "Total number of entries skipped by cursor next calls": 637299,
        "Total number of entries skipped by cursor prev calls": 8350,
        "Total number of entries skipped to position the history store cursor": 0,
        "Total number of in-memory deleted pages skipped during tree walk": 329,
        "Total number of on-disk deleted pages skipped during tree walk": 0,
        "Total number of times a search near has exited due to prefix config": 0,
        "Total number of times cursor fails to temporarily release pinned page to encourage eviction of hot or large page": 0,
        "Total number of times cursor temporarily releases pinned page to encourage eviction of hot or large page": 0,
        "bulk loaded cursor insert calls": 0,
        "cache cursors reuse count": 23854201,
        "close calls that result in cache": 23855100,
        "create calls": 4873,
        "cursor bound calls that return an error": 0,
        "cursor bounds cleared from reset": 4245655,
        "cursor bounds comparisons performed": 10,
        "cursor bounds next called on an unpositioned cursor": 2781852,
        "cursor bounds next early exit": 0,
        "cursor bounds prev called on an unpositioned cursor": 1463723,
        "cursor bounds prev early exit": 0,
        "cursor bounds search early exit": 0,
        "cursor bounds search near call repositioned cursor": 0,
        "cursor cache calls that return an error": 0,
        "cursor close calls that return an error": 0,
        "cursor compare calls that return an error": 0,
        "cursor equals calls that return an error": 0,
        "cursor get key calls that return an error": 0,
        "cursor get value calls that return an error": 0,
        "cursor insert calls that return an error": 0,
        "cursor insert check calls that return an error": 0,
        "cursor largest key calls that return an error": 0,
        "cursor modify calls that return an error": 0,
        "cursor next calls that return an error": 0,
        "cursor next calls that skip due to a globally visible history store tombstone": 0,
        "cursor next calls that skip greater than 1 and fewer than 100 entries": 426198,
        "cursor next calls that skip greater than or equal to 100 entries": 649,
        "cursor next random calls that return an error": 0,
        "cursor prev calls that return an error": 0,
        "cursor prev calls that skip due to a globally visible history store tombstone": 0,
        "cursor prev calls that skip greater than or equal to 100 entries": 0,
        "cursor prev calls that skip less than 100 entries": 6960,
        "cursor reconfigure calls that return an error": 0,
        "cursor remove calls that return an error": 0,
        "cursor reopen calls that return an error": 0,
        "cursor reserve calls that return an error": 0,
        "cursor reset calls that return an error": 0,
        "cursor search calls that return an error": 0,
        "cursor search near calls that return an error": 0,
        "cursor update calls that return an error": 0,
        "insert calls": 3003604,
        "insert key and value bytes": 2640608047,
        "modify": 0,
        "modify key and value bytes affected": 0,
        "modify value bytes modified": 0,
        "next calls": 539334110,
        "open cursor count": 0,
        "operation restarted": 5416,
        "prev calls": 8526144,
        "remove calls": 0,
        "remove key bytes removed": 0,
        "reserve calls": 0,
        "reset calls": 122987980,
        "search calls": 80332840,
        "search history store calls": 0,
        "search near calls": 154,
        "truncate calls": 77,
        "update calls": 0,
        "update key and value bytes": 0,
        "update value size change": 0
      },
      "reconciliation": {
        "VLCS pages explicitly reconciled as empty": 0,
        "approximate byte size of timestamps in pages written": 0,
        "approximate byte size of transaction IDs in pages written": 67680,
        "cursor next/prev calls during HS wrapup search_near": 0,
        "dictionary matches": 0,
        "fast-path pages deleted": 18015,
        "internal page key bytes discarded using suffix compression": 500921,
        "internal page multi-block writes": 10226,
        "leaf page key bytes discarded using prefix compression": 0,
        "leaf page multi-block writes": 6015,
        "leaf-page overflow keys": 0,
        "maximum blocks required for a page": 1,
        "overflow values written": 0,
        "page reconciliation calls": 25973,
        "page reconciliation calls for eviction": 479,
        "pages deleted": 307,
        "pages written including an aggregated newest start durable timestamp ": 167,
        "pages written including an aggregated newest stop durable timestamp ": 169,
        "pages written including an aggregated newest stop timestamp ": 13,
        "pages written including an aggregated newest stop transaction ID": 13,
        "pages written including an aggregated newest transaction ID ": 380,
        "pages written including an aggregated oldest start timestamp ": 62,
        "pages written including an aggregated prepare": 0,
        "pages written including at least one prepare": 0,
        "pages written including at least one start durable timestamp": 0,
        "pages written including at least one start timestamp": 0,
        "pages written including at least one start transaction ID": 121,
        "pages written including at least one stop durable timestamp": 0,
        "pages written including at least one stop timestamp": 0,
        "pages written including at least one stop transaction ID": 0,
        "records written including a prepare": 0,
        "records written including a start durable timestamp": 0,
        "records written including a start timestamp": 0,
        "records written including a start transaction ID": 8460,
        "records written including a stop durable timestamp": 0,
        "records written including a stop timestamp": 0,
        "records written including a stop transaction ID": 0
      },
      "session": {
        "object compaction": 0
      },
      "transaction": {
        "a reader raced with a prepared transaction commit and skipped an update or updates": 0,
        "number of times overflow removed value is read": 0,
        "race to read prepared update retry": 0,
        "rollback to stable history store keys that would have been swept in non-dryrun mode": 0,
        "rollback to stable history store records with stop timestamps older than newer records": 0,
        "rollback to stable inconsistent checkpoint": 0,
        "rollback to stable keys removed": 0,
        "rollback to stable keys restored": 0,
        "rollback to stable keys that would have been removed in non-dryrun mode": 0,
        "rollback to stable keys that would have been restored in non-dryrun mode": 0,
        "rollback to stable restored tombstones from history store": 0,
        "rollback to stable restored updates from history store": 0,
        "rollback to stable skipping delete rle": 0,
        "rollback to stable skipping stable rle": 0,
        "rollback to stable sweeping history store keys": 0,
        "rollback to stable tombstones from history store that would have been restored in non-dryrun mode": 0,
        "rollback to stable updates from history store that would have been restored in non-dryrun mode": 0,
        "rollback to stable updates removed from history store": 0,
        "rollback to stable updates that would have been removed from history store in non-dryrun mode": 0,
        "update conflicts": 0
      }
    },
    "nindexes": 0,
    "indexDetails": {},
    "indexBuilds": [],
    "totalIndexSize": 0,
    "indexSizes": {},
    "totalSize": 14620872704,
    "scaleFactor": 1,
    "_via_aggregation": true
  },
  "mysql_schemas": [
    {
      "name": "petrosa_crypto",
      "tables": [
        {
          "schema": "petrosa_crypto",
          "name": "audit_logs",
          "table_type": "BASE TABLE",
          "data_length": 1589248,
          "index_length": 1900544,
          "table_rows_estimated": 4533,
          "create_time": "2025-10-21T07:40:57",
          "update_time": null,
          "classification": "live"
        },
        {
          "schema": "petrosa_crypto",
          "name": "backfill_jobs",
          "table_type": "BASE TABLE",
          "data_length": 458752,
          "index_length": 524288,
          "table_rows_estimated": 3034,
          "create_time": "2025-10-21T07:40:58",
          "update_time": null,
          "classification": "live"
        },
        {
          "schema": "petrosa_crypto",
          "name": "contribution_summary",
          "table_type": "VIEW",
          "data_length": 0,
          "index_length": 0,
          "table_rows_estimated": 0,
          "create_time": null,
          "update_time": null,
          "classification": "view"
        },
        {
          "schema": "petrosa_crypto",
          "name": "datasets",
          "table_type": "BASE TABLE",
          "data_length": 16384,
          "index_length": 16384,
          "table_rows_estimated": 0,
          "create_time": "2025-10-21T07:40:56",
          "update_time": null,
          "classification": "live"
        },
        {
          "schema": "petrosa_crypto",
          "name": "exchange_positions",
          "table_type": "BASE TABLE",
          "data_length": 16384,
          "index_length": 32768,
          "table_rows_estimated": 0,
          "create_time": "2026-03-04T22:55:24",
          "update_time": null,
          "classification": "live"
        },
        {
          "schema": "petrosa_crypto",
          "name": "extraction_metadata",
          "table_type": "BASE TABLE",
          "data_length": 16384,
          "index_length": 16384,
          "table_rows_estimated": 0,
          "create_time": "2025-06-29T11:30:08",
          "update_time": null,
          "classification": "orphan-suspect"
        },
        {
          "schema": "petrosa_crypto",
          "name": "funding_rates",
          "table_type": "BASE TABLE",
          "data_length": 16384,
          "index_length": 49152,
          "table_rows_estimated": 0,
          "create_time": "2025-06-29T11:30:08",
          "update_time": null,
          "classification": "orphan-suspect"
        },
        {
          "schema": "petrosa_crypto",
          "name": "health_metrics",
          "table_type": "BASE TABLE",
          "data_length": 75251712,
          "index_length": 68452352,
          "table_rows_estimated": 393218,
          "create_time": "2025-10-21T07:40:57",
          "update_time": null,
          "classification": "live"
        },
        {
          "schema": "petrosa_crypto",
          "name": "klines_12h",
          "table_type": "VIEW",
          "data_length": 0,
          "index_length": 0,
          "table_rows_estimated": 0,
          "create_time": null,
          "update_time": null,
          "classification": "view"
        },
        {
          "schema": "petrosa_crypto",
          "name": "klines_15m",
          "table_type": "VIEW",
          "data_length": 0,
          "index_length": 0,
          "table_rows_estimated": 0,
          "create_time": null,
          "update_time": null,
          "classification": "view"
        },
        {
          "schema": "petrosa_crypto",
          "name": "klines_1d",
          "table_type": "VIEW",
          "data_length": 0,
          "index_length": 0,
          "table_rows_estimated": 0,
          "create_time": null,
          "update_time": null,
          "classification": "view"
        },
        {
          "schema": "petrosa_crypto",
          "name": "klines_1h",
          "table_type": "VIEW",
          "data_length": 0,
          "index_length": 0,
          "table_rows_estimated": 0,
          "create_time": null,
          "update_time": null,
          "classification": "view"
        },
        {
          "schema": "petrosa_crypto",
          "name": "klines_1m",
          "table_type": "VIEW",
          "data_length": 0,
          "index_length": 0,
          "table_rows_estimated": 0,
          "create_time": null,
          "update_time": null,
          "classification": "view"
        },
        {
          "schema": "petrosa_crypto",
          "name": "klines_2h",
          "table_type": "VIEW",
          "data_length": 0,
          "index_length": 0,
          "table_rows_estimated": 0,
          "create_time": null,
          "update_time": null,
          "classification": "view"
        },
        {
          "schema": "petrosa_crypto",
          "name": "klines_30m",
          "table_type": "VIEW",
          "data_length": 0,
          "index_length": 0,
          "table_rows_estimated": 0,
          "create_time": null,
          "update_time": null,
          "classification": "view"
        },
        {
          "schema": "petrosa_crypto",
          "name": "klines_3m",
          "table_type": "VIEW",
          "data_length": 0,
          "index_length": 0,
          "table_rows_estimated": 0,
          "create_time": null,
          "update_time": null,
          "classification": "view"
        },
        {
          "schema": "petrosa_crypto",
          "name": "klines_4h",
          "table_type": "VIEW",
          "data_length": 0,
          "index_length": 0,
          "table_rows_estimated": 0,
          "create_time": null,
          "update_time": null,
          "classification": "view"
        },
        {
          "schema": "petrosa_crypto",
          "name": "klines_5m",
          "table_type": "VIEW",
          "data_length": 0,
          "index_length": 0,
          "table_rows_estimated": 0,
          "create_time": null,
          "update_time": null,
          "classification": "view"
        },
        {
          "schema": "petrosa_crypto",
          "name": "klines_6h",
          "table_type": "VIEW",
          "data_length": 0,
          "index_length": 0,
          "table_rows_estimated": 0,
          "create_time": null,
          "update_time": null,
          "classification": "view"
        },
        {
          "schema": "petrosa_crypto",
          "name": "klines_8h",
          "table_type": "VIEW",
          "data_length": 0,
          "index_length": 0,
          "table_rows_estimated": 0,
          "create_time": null,
          "update_time": null,
          "classification": "view"
        },
        {
          "schema": "petrosa_crypto",
          "name": "klines_d1",
          "table_type": "BASE TABLE",
          "data_length": 3686400,
          "index_length": 4767744,
          "table_rows_estimated": 13604,
          "create_time": "2025-06-28T21:15:10",
          "update_time": null,
          "classification": "duplicated"
        },
        {
          "schema": "petrosa_crypto",
          "name": "klines_h1",
          "table_type": "BASE TABLE",
          "data_length": 79478784,
          "index_length": 55312384,
          "table_rows_estimated": 331388,
          "create_time": "2025-06-27T19:35:28",
          "update_time": null,
          "classification": "duplicated"
        },
        {
          "schema": "petrosa_crypto",
          "name": "klines_h12",
          "table_type": "BASE TABLE",
          "data_length": 16384,
          "index_length": 49152,
          "table_rows_estimated": 0,
          "create_time": "2026-03-13T08:19:07",
          "update_time": null,
          "classification": "live"
        },
        {
          "schema": "petrosa_crypto",
          "name": "klines_h2",
          "table_type": "BASE TABLE",
          "data_length": 16384,
          "index_length": 49152,
          "table_rows_estimated": 0,
          "create_time": "2026-03-13T08:19:05",
          "update_time": null,
          "classification": "live"
        },
        {
          "schema": "petrosa_crypto",
          "name": "klines_h4",
          "table_type": "BASE TABLE",
          "data_length": 16384,
          "index_length": 49152,
          "table_rows_estimated": 0,
          "create_time": "2026-03-13T08:19:06",
          "update_time": null,
          "classification": "live"
        },
        {
          "schema": "petrosa_crypto",
          "name": "klines_h6",
          "table_type": "BASE TABLE",
          "data_length": 16384,
          "index_length": 49152,
          "table_rows_estimated": 0,
          "create_time": "2026-03-13T08:19:06",
          "update_time": null,
          "classification": "live"
        },
        {
          "schema": "petrosa_crypto",
          "name": "klines_h8",
          "table_type": "BASE TABLE",
          "data_length": 16384,
          "index_length": 49152,
          "table_rows_estimated": 0,
          "create_time": "2026-03-13T08:19:06",
          "update_time": null,
          "classification": "live"
        },
        {
          "schema": "petrosa_crypto",
          "name": "klines_m1",
          "table_type": "BASE TABLE",
          "data_length": 16384,
          "index_length": 49152,
          "table_rows_estimated": 0,
          "create_time": "2026-03-13T08:19:04",
          "update_time": null,
          "classification": "live"
        },
        {
          "schema": "petrosa_crypto",
          "name": "klines_m15",
          "table_type": "BASE TABLE",
          "data_length": 327155712,
          "index_length": 250937344,
          "table_rows_estimated": 1466256,
          "create_time": "2025-06-27T19:36:34",
          "update_time": null,
          "classification": "duplicated"
        },
        {
          "schema": "petrosa_crypto",
          "name": "klines_m3",
          "table_type": "BASE TABLE",
          "data_length": 16384,
          "index_length": 49152,
          "table_rows_estimated": 0,
          "create_time": "2026-03-13T08:19:04",
          "update_time": null,
          "classification": "live"
        },
        {
          "schema": "petrosa_crypto",
          "name": "klines_m30",
          "table_type": "BASE TABLE",
          "data_length": 177209344,
          "index_length": 126812160,
          "table_rows_estimated": 767121,
          "create_time": "2025-06-28T21:05:10",
          "update_time": null,
          "classification": "duplicated"
        },
        {
          "schema": "petrosa_crypto",
          "name": "klines_m5",
          "table_type": "BASE TABLE",
          "data_length": 1029701632,
          "index_length": 785383424,
          "table_rows_estimated": 4688089,
          "create_time": "2025-06-28T17:40:08",
          "update_time": null,
          "classification": "duplicated"
        },
        {
          "schema": "petrosa_crypto",
          "name": "lineage_records",
          "table_type": "BASE TABLE",
          "data_length": 16384,
          "index_length": 16384,
          "table_rows_estimated": 0,
          "create_time": "2025-10-21T07:40:58",
          "update_time": null,
          "classification": "live"
        },
        {
          "schema": "petrosa_crypto",
          "name": "positions",
          "table_type": "BASE TABLE",
          "data_length": 16384,
          "index_length": 98304,
          "table_rows_estimated": 0,
          "create_time": "2026-03-02T15:04:03",
          "update_time": null,
          "classification": "live"
        },
        {
          "schema": "petrosa_crypto",
          "name": "position_contributions",
          "table_type": "BASE TABLE",
          "data_length": 16384,
          "index_length": 81920,
          "table_rows_estimated": 0,
          "create_time": "2026-03-04T22:55:24",
          "update_time": null,
          "classification": "orphan-suspect"
        },
        {
          "schema": "petrosa_crypto",
          "name": "schemas",
          "table_type": "BASE TABLE",
          "data_length": 16384,
          "index_length": 49152,
          "table_rows_estimated": 0,
          "create_time": "2025-10-23T13:10:14",
          "update_time": null,
          "classification": "live"
        },
        {
          "schema": "petrosa_crypto",
          "name": "signals",
          "table_type": "BASE TABLE",
          "data_length": 17317888,
          "index_length": 5816320,
          "table_rows_estimated": 44507,
          "create_time": "2025-10-15T00:20:38",
          "update_time": null,
          "classification": "live"
        },
        {
          "schema": "petrosa_crypto",
          "name": "strategy_performance",
          "table_type": "VIEW",
          "data_length": 0,
          "index_length": 0,
          "table_rows_estimated": 0,
          "create_time": null,
          "update_time": null,
          "classification": "view"
        },
        {
          "schema": "petrosa_crypto",
          "name": "strategy_positions",
          "table_type": "BASE TABLE",
          "data_length": 16384,
          "index_length": 98304,
          "table_rows_estimated": 0,
          "create_time": "2026-03-04T22:55:24",
          "update_time": null,
          "classification": "live"
        },
        {
          "schema": "petrosa_crypto",
          "name": "trades",
          "table_type": "BASE TABLE",
          "data_length": 16384,
          "index_length": 49152,
          "table_rows_estimated": 0,
          "create_time": "2025-06-29T11:30:07",
          "update_time": null,
          "classification": "orphan-suspect"
        }
      ],
      "error": null
    }
  ],
  "attribution": {
    "mongo_candles_bytes": 0,
    "mongo_klines_logical_bytes": 101060608,
    "mongo_klines_buckets_bytes": 0,
    "mongo_other_app_bytes": 76623872,
    "mongo_oplog_bytes": 14620872704,
    "mongo_system_dbs_bytes": 0,
    "mysql_klines_bytes": 2840903680,
    "mysql_other_bytes": 171982848
  },
  "duplicated_candle_timeframes": [
    "15m",
    "1d",
    "1h",
    "30m",
    "5m"
  ]
}
```
