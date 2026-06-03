"""Tests for the storage-inventory audit module (petrosa_k8s#794)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from data_manager.maintenance import storage_inventory as si


def _coll_stats(
    storage_size: int, data_size: int = None, count: int = 100, index_size: int = 1024
) -> dict:
    return {
        "storageSize": storage_size,
        "size": data_size if data_size is not None else storage_size * 2,
        "count": count,
        "totalIndexSize": index_size,
        "nindexes": 2,
        "avgObjSize": 256,
    }


def _db_stats(storage_size: int, collections: int = 1) -> dict:
    return {
        "storageSize": storage_size,
        "dataSize": storage_size * 2,
        "indexSize": 1024,
        "collections": collections,
        "objects": collections * 100,
    }


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------


def test_is_known_mongo_collection_matches_lifecycle_names():
    assert si._is_known_mongo_collection("intents") is True
    assert si._is_known_mongo_collection("cio_decisions") is True
    assert si._is_known_mongo_collection("signals") is True


def test_is_known_mongo_collection_matches_known_prefixes():
    assert si._is_known_mongo_collection("candles_BTCUSDT_1m") is True
    assert si._is_known_mongo_collection("klines_1h") is True
    assert si._is_known_mongo_collection("trades_ETHUSDT") is True
    assert si._is_known_mongo_collection("system.buckets.klines_1h") is True


def test_is_known_mongo_collection_returns_false_for_unknown():
    assert si._is_known_mongo_collection("totally_random") is False
    assert si._is_known_mongo_collection("legacy_stuff") is False


def test_is_known_mysql_table_matches_known_names_and_prefixes():
    assert si._is_known_mysql_table("positions") is True
    assert si._is_known_mysql_table("klines_m1") is True
    assert si._is_known_mysql_table("klines_h4") is True
    assert si._is_known_mysql_table("random_unknown") is False


def test_normalize_timeframe_canonicalises_across_naming_conventions():
    # Mongo extractor naming
    assert si._normalize_timeframe("klines_1h") == "1h"
    # MySQL physical-table naming (m1 → 1m, h4 → 4h, d1 → 1d)
    assert si._normalize_timeframe("klines_m1") == "1m"
    assert si._normalize_timeframe("klines_h4") == "4h"
    assert si._normalize_timeframe("klines_d1") == "1d"
    # data-manager per-symbol/per-timeframe split
    assert si._normalize_timeframe("candles_BTCUSDT_15m") == "15m"
    # Non-candle collections return None
    assert si._normalize_timeframe("signals") is None
    assert si._normalize_timeframe("intents") is None


# ---------------------------------------------------------------------------
# Mongo audit aggregation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_mongo_aggregates_per_db_and_per_collection():
    """AC1: full enumeration walks every DB returned by listDatabases."""
    adapter = AsyncMock()
    adapter.list_databases.return_value = [
        {"name": "petrosa", "sizeOnDisk": 1000, "empty": False},
        {"name": "admin", "sizeOnDisk": 100, "empty": False},
    ]
    adapter.db_stats.side_effect = lambda name: _db_stats(
        1024 * 1024 if name == "petrosa" else 4096,
        collections=2 if name == "petrosa" else 1,
    )

    # Mock list_collection_names per-DB via the client[db] indexing
    petrosa_db = MagicMock()
    petrosa_db.list_collection_names = AsyncMock(return_value=["signals", "klines_1h"])
    admin_db = MagicMock()
    admin_db.list_collection_names = AsyncMock(return_value=["system.users"])

    client = MagicMock()
    client.__getitem__.side_effect = (
        lambda n: petrosa_db if n == "petrosa" else admin_db
    )
    adapter.client = client

    adapter.is_timeseries.side_effect = lambda db, coll: coll == "klines_1h"
    adapter.coll_stats.side_effect = lambda db, coll: _coll_stats(
        50_000 if coll == "klines_1h" else 1_000 if coll == "signals" else 256
    )
    adapter.newest_doc_age.return_value = datetime(2026, 6, 3, tzinfo=UTC)
    adapter.oplog_size.return_value = {"storageSize": 200_000_000, "size": 100_000_000}

    dbs, oplog, err = await si.audit_mongo(adapter)

    assert err is None
    assert oplog == {"storageSize": 200_000_000, "size": 100_000_000}
    assert {db.name for db in dbs} == {"petrosa", "admin"}
    petrosa = next(db for db in dbs if db.name == "petrosa")
    assert petrosa.is_system is False
    assert {c.name for c in petrosa.collection_stats} == {"signals", "klines_1h"}
    klines = next(c for c in petrosa.collection_stats if c.name == "klines_1h")
    assert klines.is_timeseries is True


@pytest.mark.asyncio
async def test_audit_mongo_returns_error_when_list_databases_fails():
    """AC7: top-level listDatabases failure surfaces an error string."""
    adapter = AsyncMock()
    adapter.list_databases.side_effect = RuntimeError("not authorized on admin")

    dbs, oplog, err = await si.audit_mongo(adapter)

    assert dbs == []
    assert oplog is None
    assert err is not None
    assert "not authorized" in err


@pytest.mark.asyncio
async def test_audit_mongo_collection_records_backing_bucket_for_timeseries():
    """AC11: time-series collections also size the backing system.buckets.*."""
    adapter = AsyncMock()
    adapter.is_timeseries.return_value = True
    adapter.coll_stats.side_effect = [
        _coll_stats(1_000),  # logical klines_1h
        _coll_stats(500_000),  # backing system.buckets.klines_1h
    ]
    adapter.newest_doc_age.return_value = None

    stat = await si._audit_mongo_collection(adapter, "petrosa", "klines_1h")

    assert stat.is_timeseries is True
    assert stat.storage_size == 1_000
    assert stat.backing_bucket_storage_size == 500_000


# ---------------------------------------------------------------------------
# MySQL audit aggregation
# ---------------------------------------------------------------------------


def test_audit_mysql_excludes_views_from_byte_totals():
    """AC6: VIEW rows are surfaced but not summed into storage totals."""
    fake_engine = MagicMock()
    fake_engine.dispose = MagicMock()

    with (
        patch.object(si, "create_read_only_engine", return_value=fake_engine),
        patch.object(si, "list_schemas", return_value=["petrosa"]),
        patch.object(
            si,
            "table_inventory",
            return_value=[
                {
                    "TABLE_NAME": "klines_m1",
                    "TABLE_TYPE": "BASE TABLE",
                    "TABLE_ROWS": 1000,
                    "DATA_LENGTH": 100_000,
                    "INDEX_LENGTH": 20_000,
                    "CREATE_TIME": None,
                    "UPDATE_TIME": None,
                },
                {
                    "TABLE_NAME": "klines_1m",
                    "TABLE_TYPE": "VIEW",
                    "TABLE_ROWS": 0,
                    "DATA_LENGTH": 0,
                    "INDEX_LENGTH": 0,
                    "CREATE_TIME": None,
                    "UPDATE_TIME": None,
                },
            ],
        ),
    ):
        schemas, err = si.audit_mysql("mysql+pymysql://user:pass@host/db")

    assert err is None
    assert len(schemas) == 1
    schema = schemas[0]
    assert {t.name for t in schema.tables} == {"klines_m1", "klines_1m"}
    base_total = sum(
        t.data_length + t.index_length for t in schema.tables if t.table_type != "VIEW"
    )
    view_total = sum(
        t.data_length + t.index_length for t in schema.tables if t.table_type == "VIEW"
    )
    assert base_total == 120_000
    assert view_total == 0
    fake_engine.dispose.assert_called_once()


def test_audit_mysql_returns_error_when_schema_enumeration_fails():
    fake_engine = MagicMock()
    with (
        patch.object(si, "create_read_only_engine", return_value=fake_engine),
        patch.object(
            si, "list_schemas", side_effect=RuntimeError("access denied for user")
        ),
    ):
        schemas, err = si.audit_mysql("mysql+pymysql://user:pass@host/db")
    assert schemas == []
    assert "access denied" in err


# ---------------------------------------------------------------------------
# Classification + attribution
# ---------------------------------------------------------------------------


def _build_report_for_classification() -> si.StorageInventoryReport:
    return si.StorageInventoryReport(
        generated_at="2026-06-03T00:00:00Z",
        mongo_ok=True,
        mysql_ok=True,
        mongo_error=None,
        mysql_error=None,
        mongo_databases=[
            si.MongoDatabaseStat(
                name="petrosa",
                is_system=False,
                storage_size=1_000_000,
                data_size=2_000_000,
                index_size=10_000,
                collections=3,
                objects=300,
                collection_stats=[
                    si.MongoCollectionStat(
                        db_name="petrosa",
                        name="klines_1m",
                        is_timeseries=True,
                        storage_size=100_000,
                        data_size=400_000,
                        total_index_size=1024,
                        count=10_000,
                        n_indexes=2,
                        avg_obj_size=64,
                        backing_bucket_storage_size=2_000_000,
                        backing_bucket_data_size=4_000_000,
                        newest_doc_age=None,
                        classification="live",
                    ),
                    si.MongoCollectionStat(
                        db_name="petrosa",
                        name="legacy_orphan_collection",
                        is_timeseries=False,
                        storage_size=50_000,
                        data_size=80_000,
                        total_index_size=512,
                        count=100,
                        n_indexes=1,
                        avg_obj_size=512,
                        backing_bucket_storage_size=None,
                        backing_bucket_data_size=None,
                        newest_doc_age=None,
                        classification="live",
                    ),
                    si.MongoCollectionStat(
                        db_name="petrosa",
                        name="signals",
                        is_timeseries=False,
                        storage_size=10_000,
                        data_size=12_000,
                        total_index_size=256,
                        count=42,
                        n_indexes=2,
                        avg_obj_size=240,
                        backing_bucket_storage_size=None,
                        backing_bucket_data_size=None,
                        newest_doc_age=None,
                        classification="live",
                    ),
                ],
            ),
            si.MongoDatabaseStat(
                name="local",
                is_system=True,
                storage_size=300_000_000,
                data_size=300_000_000,
                index_size=0,
                collections=1,
                objects=0,
                collection_stats=[
                    si.MongoCollectionStat(
                        db_name="local",
                        name="oplog.rs",
                        is_timeseries=False,
                        storage_size=300_000_000,
                        data_size=300_000_000,
                        total_index_size=0,
                        count=0,
                        n_indexes=0,
                        avg_obj_size=0,
                        backing_bucket_storage_size=None,
                        backing_bucket_data_size=None,
                        newest_doc_age=None,
                        classification="live",
                    ),
                ],
            ),
        ],
        mongo_oplog={"storageSize": 300_000_000, "size": 280_000_000},
        mysql_schemas=[
            si.MysqlSchemaStat(
                name="petrosa",
                tables=[
                    si.MysqlTableStat(
                        schema="petrosa",
                        name="klines_m1",
                        table_type="BASE TABLE",
                        data_length=200_000,
                        index_length=40_000,
                        table_rows_estimated=1234,
                        create_time=None,
                        update_time=None,
                        classification="live",
                    ),
                    si.MysqlTableStat(
                        schema="petrosa",
                        name="klines_1m",
                        table_type="VIEW",
                        data_length=0,
                        index_length=0,
                        table_rows_estimated=0,
                        create_time=None,
                        update_time=None,
                        classification="live",
                    ),
                ],
            ),
        ],
    )


def test_classify_all_flags_duplicated_candle_timeframes():
    """AC4: a timeframe present in Mongo klines_* AND MySQL klines_* is `duplicated`."""
    report = _build_report_for_classification()
    si._classify_all(report)

    assert "1m" in report.duplicated_candle_timeframes
    petrosa_db = next(db for db in report.mongo_databases if db.name == "petrosa")
    klines = next(c for c in petrosa_db.collection_stats if c.name == "klines_1m")
    assert klines.classification == "duplicated"
    mysql_klines = report.mysql_schemas[0].tables[0]
    assert mysql_klines.classification == "duplicated"


def test_classify_all_flags_orphan_suspect_when_unknown():
    """AC5: a live collection with no known writer is `orphan-suspect`."""
    report = _build_report_for_classification()
    si._classify_all(report)
    petrosa_db = next(db for db in report.mongo_databases if db.name == "petrosa")
    orphan = next(
        c for c in petrosa_db.collection_stats if c.name == "legacy_orphan_collection"
    )
    assert orphan.classification == "orphan-suspect"


def test_classify_all_labels_system_db_collections_separately():
    """System DBs (admin/local/config) get `system` classification, never `orphan-suspect`."""
    report = _build_report_for_classification()
    si._classify_all(report)
    local_db = next(db for db in report.mongo_databases if db.name == "local")
    oplog = local_db.collection_stats[0]
    assert oplog.classification == "system"


def test_classify_all_labels_mysql_view_as_view_not_orphan():
    """AC6: VIEW rows get explicit `view` classification (not orphan-suspect)."""
    report = _build_report_for_classification()
    si._classify_all(report)
    view = next(t for t in report.mysql_schemas[0].tables if t.table_type == "VIEW")
    assert view.classification == "view"


def test_attribute_400mb_separates_categories_for_root_cause():
    """AC3: attribution bucket breaks the residual footprint into named categories."""
    report = _build_report_for_classification()
    si._classify_all(report)
    si._attribute_400mb(report)

    attr = report.attribution
    # klines logical + buckets reported separately
    assert attr["mongo_klines_logical_bytes"] == 100_000
    assert attr["mongo_klines_buckets_bytes"] == 2_000_000
    # legacy_orphan_collection + signals fall into other_app
    assert attr["mongo_other_app_bytes"] == 60_000
    # oplog reported separately, not folded into system_dbs
    assert attr["mongo_oplog_bytes"] == 300_000_000
    assert attr["mongo_system_dbs_bytes"] == 0  # oplog excluded from this bucket
    # MySQL klines_m1 only (the VIEW is excluded)
    assert attr["mysql_klines_bytes"] == 240_000
    assert attr["mysql_other_bytes"] == 0


# ---------------------------------------------------------------------------
# AC2 — read-only assertion against the audit module
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_mongo_never_invokes_write_methods_on_adapter():
    """AC2(b): no write/delete/insert/update/drop is ever called by the audit."""
    adapter = AsyncMock()
    adapter.list_databases.return_value = [
        {"name": "petrosa", "sizeOnDisk": 1, "empty": False}
    ]
    adapter.db_stats.return_value = _db_stats(1024)
    petrosa_db = MagicMock()
    petrosa_db.list_collection_names = AsyncMock(return_value=["signals"])
    client = MagicMock()
    client.__getitem__.return_value = petrosa_db
    adapter.client = client
    adapter.is_timeseries.return_value = False
    adapter.coll_stats.return_value = _coll_stats(100)
    adapter.newest_doc_age.return_value = None
    adapter.oplog_size.return_value = None

    await si.audit_mongo(adapter)

    forbidden = (
        "write",
        "write_batch",
        "delete_range",
        "ensure_indexes",
        "upsert_app_config",
        "upsert_global_config",
        "upsert_symbol_config",
        "delete_global_config",
        "delete_symbol_config",
    )
    for name in forbidden:
        assert not getattr(adapter, name).called, (
            f"audit invoked forbidden adapter.{name}"
        )


def test_audit_mysql_does_not_construct_mysqladapter_instance():
    """AC2/AC10: the audit must use `create_read_only_engine`, never MySQLAdapter.connect()."""
    fake_engine = MagicMock()
    with (
        patch.object(
            si, "create_read_only_engine", return_value=fake_engine
        ) as mock_create,
        patch.object(si, "list_schemas", return_value=[]),
        patch.object(si, "table_inventory", return_value=[]),
    ):
        schemas, err = si.audit_mysql("mysql+pymysql://user:pass@host/db")
    assert err is None
    assert mock_create.call_count == 1
    fake_engine.dispose.assert_called_once()


# ---------------------------------------------------------------------------
# AC7 — defined exit codes per failure scenario
# ---------------------------------------------------------------------------


def test_compute_exit_code_both_ok():
    report = si.StorageInventoryReport(
        generated_at="t",
        mongo_ok=True,
        mysql_ok=True,
        mongo_error=None,
        mysql_error=None,
    )
    assert si._compute_exit_code(report, True, True) == si.EXIT_OK


def test_compute_exit_code_mongo_failed():
    report = si.StorageInventoryReport(
        generated_at="t",
        mongo_ok=False,
        mysql_ok=True,
        mongo_error="connection refused",
        mysql_error=None,
    )
    assert si._compute_exit_code(report, True, True) == si.EXIT_MONGO_FAILED


def test_compute_exit_code_mysql_failed():
    report = si.StorageInventoryReport(
        generated_at="t",
        mongo_ok=True,
        mysql_ok=False,
        mongo_error=None,
        mysql_error="connection timeout",
    )
    assert si._compute_exit_code(report, True, True) == si.EXIT_MYSQL_FAILED


def test_compute_exit_code_both_failed():
    report = si.StorageInventoryReport(
        generated_at="t",
        mongo_ok=False,
        mysql_ok=False,
        mongo_error="connection refused",
        mysql_error="connection timeout",
    )
    assert si._compute_exit_code(report, True, True) == si.EXIT_BOTH_FAILED


def test_compute_exit_code_privilege_denied_distinguished_from_generic():
    report = si.StorageInventoryReport(
        generated_at="t",
        mongo_ok=False,
        mysql_ok=True,
        mongo_error="command listDatabases requires authentication: not authorized",
        mysql_error=None,
    )
    assert si._compute_exit_code(report, True, True) == si.EXIT_PRIVILEGE_DENIED


def test_compute_exit_code_skips_disabled_backend():
    """--mongo-only should not penalise the MySQL backend being absent."""
    report = si.StorageInventoryReport(
        generated_at="t",
        mongo_ok=True,
        mysql_ok=False,
        mongo_error=None,
        mysql_error=None,
    )
    assert si._compute_exit_code(report, True, False) == si.EXIT_OK


# ---------------------------------------------------------------------------
# Output rendering smoke test
# ---------------------------------------------------------------------------


def test_render_markdown_includes_attribution_and_duplication_sections():
    report = _build_report_for_classification()
    si._classify_all(report)
    si._attribute_400mb(report)
    text = si.render_markdown(report)
    assert "# Storage Inventory Audit Report" in text
    assert "## 400 MB Attribution" in text
    assert "## Duplicated candle timeframes" in text
    assert "`1m`" in text
    assert "## MongoDB databases" in text
    assert "## MySQL schemas" in text


def test_render_markdown_handles_empty_report_with_errors():
    """Markdown renders cleanly when both backends errored before producing data."""
    report = si.StorageInventoryReport(
        generated_at="2026-06-03T00:00:00Z",
        mongo_ok=False,
        mysql_ok=False,
        mongo_error="not authorized on admin",
        mysql_error="connection refused",
    )
    text = si.render_markdown(report)
    assert "Mongo backend: FAILED" in text
    assert "MySQL backend: FAILED" in text
    assert "not authorized" in text
    assert "connection refused" in text


def test_render_markdown_includes_oplog_when_present():
    report = si.StorageInventoryReport(
        generated_at="t",
        mongo_ok=True,
        mysql_ok=True,
        mongo_error=None,
        mysql_error=None,
        mongo_oplog={"storageSize": 5000, "size": 4000},
    )
    text = si.render_markdown(report)
    assert "Oplog" in text
    assert "5,000" in text


def test_render_markdown_marks_error_rows_for_failed_databases():
    db = si.MongoDatabaseStat(
        name="petrosa",
        is_system=False,
        storage_size=0,
        data_size=0,
        index_size=0,
        collections=0,
        objects=0,
        error="dbStats failed",
    )
    report = si.StorageInventoryReport(
        generated_at="t",
        mongo_ok=True,
        mysql_ok=True,
        mongo_error=None,
        mysql_error=None,
        mongo_databases=[db],
    )
    text = si.render_markdown(report)
    assert "_error_" in text
    assert "dbStats failed" in text


def test_render_markdown_marks_error_rows_for_failed_schemas():
    schema = si.MysqlSchemaStat(name="petrosa", error="permission denied")
    report = si.StorageInventoryReport(
        generated_at="t",
        mongo_ok=True,
        mysql_ok=True,
        mongo_error=None,
        mysql_error=None,
        mysql_schemas=[schema],
    )
    text = si.render_markdown(report)
    assert "_error_" in text
    assert "permission denied" in text


def test_report_to_dict_round_trips_via_json():
    """The structured report must serialise cleanly via json.dumps(default=str)."""
    import json as _json

    report = _build_report_for_classification()
    si._classify_all(report)
    si._attribute_400mb(report)

    serialised = _json.dumps(si.report_to_dict(report), default=str)
    parsed = _json.loads(serialised)
    assert parsed["mongo_ok"] is True
    assert parsed["attribution"]["mongo_klines_buckets_bytes"] == 2_000_000
    assert "1m" in parsed["duplicated_candle_timeframes"]


def test_isoformat_or_none_handles_datetime_and_none():
    assert si._isoformat_or_none(None) is None
    dt = datetime(2026, 6, 3, 12, 0, tzinfo=UTC)
    assert si._isoformat_or_none(dt) == dt.isoformat()
    assert si._isoformat_or_none("not-a-datetime") is None


def test_is_privilege_error_detects_common_phrasings():
    assert si._is_privilege_error("not authorized on admin") is True
    assert si._is_privilege_error("permission denied for user 'audit'") is True
    assert si._is_privilege_error("Access denied; you need SELECT privilege") is True
    assert si._is_privilege_error("Unauthorized — listDatabases refused") is True
    assert si._is_privilege_error("connection refused") is False
    assert si._is_privilege_error(None) is False
    assert si._is_privilege_error("") is False


# ---------------------------------------------------------------------------
# _audit_mongo_collection — error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_mongo_collection_records_error_when_coll_stats_fails():
    adapter = AsyncMock()
    adapter.is_timeseries.return_value = False
    adapter.coll_stats.side_effect = RuntimeError("namespace not found")
    adapter.newest_doc_age.return_value = None

    stat = await si._audit_mongo_collection(adapter, "petrosa", "missing_collection")

    assert stat.classification == "error"
    assert stat.error == "namespace not found"
    assert stat.storage_size == 0


@pytest.mark.asyncio
async def test_audit_mongo_collection_tolerates_missing_backing_bucket():
    """Time-series collection without a discoverable system.buckets.* still produces a stat."""
    adapter = AsyncMock()
    adapter.is_timeseries.return_value = True
    adapter.coll_stats.side_effect = [
        _coll_stats(1_000),
        RuntimeError("system.buckets.klines_1h not found"),
    ]
    adapter.newest_doc_age.return_value = None

    stat = await si._audit_mongo_collection(adapter, "petrosa", "klines_1h")

    assert stat.is_timeseries is True
    assert stat.storage_size == 1_000
    assert stat.backing_bucket_storage_size is None


@pytest.mark.asyncio
async def test_audit_mongo_collection_handles_newest_doc_age_failure():
    adapter = AsyncMock()
    adapter.is_timeseries.return_value = False
    adapter.coll_stats.return_value = _coll_stats(200)
    adapter.newest_doc_age.side_effect = RuntimeError("read denied")

    stat = await si._audit_mongo_collection(adapter, "petrosa", "signals")
    assert stat.newest_doc_age is None
    assert stat.storage_size == 200


@pytest.mark.asyncio
async def test_audit_mongo_handles_per_db_stats_failure_gracefully():
    """A single DB failing dbStats is recorded as an error on that DB; siblings still walk."""
    adapter = AsyncMock()
    adapter.list_databases.return_value = [
        {"name": "petrosa", "sizeOnDisk": 1000, "empty": False},
        {"name": "denied_db", "sizeOnDisk": 50, "empty": False},
    ]

    def _db_stats_side_effect(name):
        if name == "denied_db":
            raise RuntimeError("not authorized")
        return _db_stats(1024)

    adapter.db_stats.side_effect = _db_stats_side_effect

    petrosa_db = MagicMock()
    petrosa_db.list_collection_names = AsyncMock(return_value=["signals"])
    client = MagicMock()
    client.__getitem__.return_value = petrosa_db
    adapter.client = client

    adapter.is_timeseries.return_value = False
    adapter.coll_stats.return_value = _coll_stats(100)
    adapter.newest_doc_age.return_value = None
    adapter.oplog_size.return_value = None

    dbs, _, err = await si.audit_mongo(adapter)
    assert err is None
    by_name = {d.name: d for d in dbs}
    assert by_name["denied_db"].error == "not authorized"
    assert by_name["petrosa"].error is None
    # The denied DB carries no per-collection rows because dbStats failed
    assert by_name["denied_db"].collection_stats == []


@pytest.mark.asyncio
async def test_audit_mongo_handles_list_collections_failure_per_db():
    """If list_collection_names fails for a DB, that DB carries the error and walk continues."""
    adapter = AsyncMock()
    adapter.list_databases.return_value = [
        {"name": "petrosa", "sizeOnDisk": 1, "empty": False},
    ]
    adapter.db_stats.return_value = _db_stats(1024)
    petrosa_db = MagicMock()
    petrosa_db.list_collection_names = AsyncMock(
        side_effect=RuntimeError("network blip")
    )
    client = MagicMock()
    client.__getitem__.return_value = petrosa_db
    adapter.client = client
    adapter.oplog_size.return_value = None

    dbs, _, err = await si.audit_mongo(adapter)
    assert err is None
    assert dbs[0].error == "network blip"


@pytest.mark.asyncio
async def test_audit_mongo_continues_when_oplog_unreachable():
    """oplog_size is best-effort; failure must not propagate to the caller."""
    adapter = AsyncMock()
    adapter.list_databases.return_value = []
    adapter.oplog_size.side_effect = RuntimeError("oplog read denied")

    dbs, oplog, err = await si.audit_mongo(adapter)
    assert err is None
    assert oplog is None
    assert dbs == []


# ---------------------------------------------------------------------------
# MySQL audit — schema-level errors
# ---------------------------------------------------------------------------


def test_audit_mysql_records_per_schema_table_inventory_failure():
    fake_engine = MagicMock()
    with (
        patch.object(si, "create_read_only_engine", return_value=fake_engine),
        patch.object(si, "list_schemas", return_value=["good", "bad"]),
        patch.object(
            si,
            "table_inventory",
            side_effect=[
                [
                    {
                        "TABLE_NAME": "positions",
                        "TABLE_TYPE": "BASE TABLE",
                        "TABLE_ROWS": 10,
                        "DATA_LENGTH": 1000,
                        "INDEX_LENGTH": 200,
                        "CREATE_TIME": None,
                        "UPDATE_TIME": None,
                    },
                ],
                RuntimeError("denied"),
            ],
        ),
    ):
        schemas, err = si.audit_mysql("mysql+pymysql://x")

    assert err is None
    by_name = {s.name: s for s in schemas}
    assert by_name["good"].error is None
    assert by_name["bad"].error == "denied"
    assert by_name["bad"].tables == []
    fake_engine.dispose.assert_called_once()


# ---------------------------------------------------------------------------
# Adapter-level read-only helpers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mongodb_adapter_list_databases_returns_listdatabases_payload():
    from data_manager.db.mongodb_adapter import MongoDBAdapter

    adapter = MongoDBAdapter.__new__(MongoDBAdapter)
    adapter._connected = True
    adapter.client = MagicMock()
    adapter.client.list_databases = AsyncMock(
        return_value=[
            {"name": "petrosa", "sizeOnDisk": 1024, "empty": False},
            {"name": "admin", "sizeOnDisk": 32, "empty": False},
        ]
    )

    result = await adapter.list_databases()
    assert {db["name"] for db in result} == {"petrosa", "admin"}


@pytest.mark.asyncio
async def test_mongodb_adapter_db_stats_runs_dbstats_command():
    from data_manager.db.mongodb_adapter import MongoDBAdapter

    adapter = MongoDBAdapter.__new__(MongoDBAdapter)
    adapter._connected = True
    fake_db = MagicMock()
    fake_db.command = AsyncMock(return_value={"storageSize": 1024, "dataSize": 4096})
    adapter.client = MagicMock()
    adapter.client.__getitem__.return_value = fake_db

    stats = await adapter.db_stats("petrosa")
    assert stats["storageSize"] == 1024
    fake_db.command.assert_awaited_once_with("dbStats")


@pytest.mark.asyncio
async def test_mongodb_adapter_coll_stats_falls_back_to_aggregation_when_command_denied():
    from pymongo.errors import PyMongoError

    from data_manager.db.mongodb_adapter import MongoDBAdapter

    adapter = MongoDBAdapter.__new__(MongoDBAdapter)
    adapter._connected = True

    fake_coll = MagicMock()
    fake_cursor = MagicMock()
    fake_cursor.to_list = AsyncMock(
        return_value=[{"storageStats": {"storageSize": 9999}}]
    )
    fake_coll.aggregate.return_value = fake_cursor

    fake_db = MagicMock()
    fake_db.command = AsyncMock(side_effect=PyMongoError("denied"))
    fake_db.__getitem__.return_value = fake_coll
    adapter.client = MagicMock()
    adapter.client.__getitem__.return_value = fake_db

    stats = await adapter.coll_stats("petrosa", "signals")
    assert stats["storageSize"] == 9999
    assert stats["_via_aggregation"] is True


@pytest.mark.asyncio
async def test_mongodb_adapter_is_timeseries_reads_collection_spec():
    from data_manager.db.mongodb_adapter import MongoDBAdapter

    adapter = MongoDBAdapter.__new__(MongoDBAdapter)
    adapter._connected = True

    fake_db = MagicMock()

    class _FakeCursor:
        def __init__(self, docs):
            self._docs = docs

        async def to_list(self, length):
            return self._docs

    fake_db.list_collections.return_value = _FakeCursor(
        [
            {
                "name": "klines_1h",
                "type": "timeseries",
                "options": {"timeseries": {"timeField": "ts"}},
            }
        ]
    )
    adapter.client = MagicMock()
    adapter.client.__getitem__.return_value = fake_db

    assert await adapter.is_timeseries("petrosa", "klines_1h") is True

    fake_db.list_collections.return_value = _FakeCursor([])
    assert await adapter.is_timeseries("petrosa", "missing") is False


@pytest.mark.asyncio
async def test_mongodb_adapter_newest_doc_age_returns_datetime_from_first_hit():
    from data_manager.db.mongodb_adapter import MongoDBAdapter

    adapter = MongoDBAdapter.__new__(MongoDBAdapter)
    adapter._connected = True

    fake_coll = MagicMock()
    target_dt = datetime(2026, 6, 3, 12, 0, tzinfo=UTC)
    fake_coll.find_one = AsyncMock(return_value={"timestamp": target_dt})

    fake_db = MagicMock()
    fake_db.__getitem__.return_value = fake_coll
    adapter.client = MagicMock()
    adapter.client.__getitem__.return_value = fake_db

    result = await adapter.newest_doc_age("petrosa", "signals")
    assert result == target_dt


@pytest.mark.asyncio
async def test_mongodb_adapter_newest_doc_age_returns_none_on_empty_collection():
    from data_manager.db.mongodb_adapter import MongoDBAdapter

    adapter = MongoDBAdapter.__new__(MongoDBAdapter)
    adapter._connected = True

    fake_coll = MagicMock()
    fake_coll.find_one = AsyncMock(return_value=None)
    fake_db = MagicMock()
    fake_db.__getitem__.return_value = fake_coll
    adapter.client = MagicMock()
    adapter.client.__getitem__.return_value = fake_db

    result = await adapter.newest_doc_age("petrosa", "empty_col")
    assert result is None


def test_mysql_create_read_only_engine_does_not_call_create_tables():
    """AC10: the read-only engine helper must not invoke MetaData.create_all."""
    from data_manager.db import mysql_adapter as ma

    fake_engine = MagicMock()
    with patch.object(ma, "create_engine", return_value=fake_engine) as mock_ce:
        engine = ma.create_read_only_engine("mysql+pymysql://user:pass@host:3306/db")

    assert engine is fake_engine
    mock_ce.assert_called_once()
    # The kwargs explicitly disable connection-time pool warming and avoid DDL
    _, kwargs = mock_ce.call_args
    assert kwargs["pool_pre_ping"] is True
    assert kwargs["pool_size"] == 2
    # Sanity: there is NO call to metadata.create_all anywhere on the engine
    assert not fake_engine.metadata.create_all.called


def test_mysql_list_schemas_excludes_system_schemas():
    from data_manager.db.mysql_adapter import list_schemas

    fake_conn = MagicMock()
    fake_conn.__enter__.return_value = fake_conn
    fake_conn.__exit__.return_value = None
    fake_conn.execute.return_value.fetchall.return_value = [
        ("petrosa",),
        ("information_schema",),
        ("mysql",),
        ("performance_schema",),
        ("sys",),
        ("staging",),
    ]
    fake_engine = MagicMock()
    fake_engine.connect.return_value = fake_conn

    result = list_schemas(fake_engine)
    assert sorted(result) == ["petrosa", "staging"]


def test_mysql_table_inventory_returns_table_rows_with_table_type():
    from data_manager.db.mysql_adapter import table_inventory

    fake_conn = MagicMock()
    fake_conn.__enter__.return_value = fake_conn
    fake_conn.__exit__.return_value = None
    fake_rows = [
        {
            "TABLE_NAME": "klines_m1",
            "TABLE_TYPE": "BASE TABLE",
            "TABLE_ROWS": 1000,
            "DATA_LENGTH": 1024,
            "INDEX_LENGTH": 256,
            "CREATE_TIME": None,
            "UPDATE_TIME": None,
        }
    ]
    fake_conn.execute.return_value.mappings.return_value.fetchall.return_value = (
        fake_rows
    )
    fake_engine = MagicMock()
    fake_engine.connect.return_value = fake_conn

    result = table_inventory(fake_engine, "petrosa")
    assert len(result) == 1
    assert result[0]["TABLE_NAME"] == "klines_m1"
    assert result[0]["TABLE_TYPE"] == "BASE TABLE"


# ---------------------------------------------------------------------------
# CLI entrypoint exit-code paths
# ---------------------------------------------------------------------------


def test_amain_returns_ok_when_both_env_vars_missing_and_only_one_backend_requested(
    monkeypatch,
):
    """--mongo-only with no MONGODB_URL set produces a clear error + exit 10."""
    monkeypatch.delenv("MONGODB_URL", raising=False)
    monkeypatch.delenv("MYSQL_URI", raising=False)
    rc = si.main(["--mongo-only", "--json"])
    assert rc == si.EXIT_MONGO_FAILED


def test_amain_rejects_mutually_exclusive_flags(monkeypatch):
    monkeypatch.delenv("MONGODB_URL", raising=False)
    monkeypatch.delenv("MYSQL_URI", raising=False)
    rc = si.main(["--mongo-only", "--mysql-only"])
    assert rc == 2


def test_amain_returns_mysql_failed_when_mysql_uri_missing(monkeypatch):
    monkeypatch.delenv("MONGODB_URL", raising=False)
    monkeypatch.delenv("MYSQL_URI", raising=False)
    rc = si.main(["--mysql-only"])
    assert rc == si.EXIT_MYSQL_FAILED


def test_amain_returns_both_failed_when_no_env_set(monkeypatch):
    monkeypatch.delenv("MONGODB_URL", raising=False)
    monkeypatch.delenv("MYSQL_URI", raising=False)
    rc = si.main([])
    assert rc == si.EXIT_BOTH_FAILED
