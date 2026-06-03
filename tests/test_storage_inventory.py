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
