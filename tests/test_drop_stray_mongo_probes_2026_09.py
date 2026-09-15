"""Unit tests for
`data_manager.maintenance.drop_stray_mongo_probes_2026_09` (data-manager#301).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from data_manager.maintenance import drop_stray_mongo_probes_2026_09 as mod


def _now() -> datetime:
    return datetime(2026, 9, 15, tzinfo=UTC)


def _make_adapter_client(
    *, db_collections: dict[str, dict[str, list[dict]]]
) -> MagicMock:
    """Build a fake motor-style client: client[db][collection] returns a
    fake collection object backed by an in-memory doc list, supporting
    count_documents / list_collection_names / drop / delete_many."""
    client = MagicMock()

    db_cache: dict[str, MagicMock] = {}
    coll_cache: dict[tuple[str, str], MagicMock] = {}

    def _get_db(db_name: str) -> MagicMock:
        if db_name in db_cache:
            return db_cache[db_name]
        db_mock = MagicMock()
        colls = db_collections.get(db_name, {})

        async def _list_collection_names() -> list[str]:
            return list(colls.keys())

        db_mock.list_collection_names = AsyncMock(side_effect=_list_collection_names)

        def _get_collection(coll_name: str, _db_name: str = db_name) -> MagicMock:
            cache_key = (_db_name, coll_name)
            if cache_key in coll_cache:
                return coll_cache[cache_key]
            coll_mock = MagicMock()
            docs = colls.get(coll_name, [])

            async def _count_documents(filt: dict) -> int:
                if not filt:
                    return len(docs)
                cutoff = filt.get("created_at", {}).get("$lt")
                if cutoff is None:
                    return len(docs)
                return sum(1 for d in docs if d["created_at"] < cutoff)

            async def _delete_many(filt: dict):
                cutoff = filt["created_at"]["$lt"]
                to_delete = [d for d in docs if d["created_at"] < cutoff]
                for d in to_delete:
                    docs.remove(d)
                result = MagicMock()
                result.deleted_count = len(to_delete)
                return result

            coll_mock.count_documents = AsyncMock(side_effect=_count_documents)
            coll_mock.delete_many = AsyncMock(side_effect=_delete_many)
            coll_mock.drop = AsyncMock()
            coll_cache[cache_key] = coll_mock
            return coll_mock

        db_mock.__getitem__ = MagicMock(side_effect=_get_collection)
        db_cache[db_name] = db_mock
        return db_mock

    client.__getitem__ = MagicMock(side_effect=_get_db)
    return client


class TestDropZeroDocCollection:
    @pytest.mark.asyncio
    async def test_dry_run_reports_would_drop_when_empty(self):
        adapter = MagicMock()
        adapter.client = _make_adapter_client(
            db_collections={"petrosa_data_manager": {"_atlas_quota_probe": []}}
        )

        result = await mod.drop_zero_doc_collection(
            adapter, "petrosa_data_manager", "_atlas_quota_probe", dry_run=True
        )

        assert result.existed is True
        assert result.doc_count == 0
        assert result.guard_tripped is False
        assert result.dropped is False

    @pytest.mark.asyncio
    async def test_apply_drops_when_empty(self):
        adapter = MagicMock()
        adapter.client = _make_adapter_client(
            db_collections={"petrosa_data_manager": {"_atlas_quota_probe": []}}
        )

        result = await mod.drop_zero_doc_collection(
            adapter, "petrosa_data_manager", "_atlas_quota_probe", dry_run=False
        )

        assert result.dropped is True
        adapter.client["petrosa_data_manager"][
            "_atlas_quota_probe"
        ].drop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_apply_refuses_when_docs_present(self):
        adapter = MagicMock()
        adapter.client = _make_adapter_client(
            db_collections={
                "petrosa_data_manager": {"_atlas_quota_probe": [{"created_at": "x"}]}
            }
        )

        result = await mod.drop_zero_doc_collection(
            adapter, "petrosa_data_manager", "_atlas_quota_probe", dry_run=False
        )

        assert result.dropped is False
        assert result.guard_tripped is True
        assert result.doc_count == 1

    @pytest.mark.asyncio
    async def test_absent_collection_is_noop(self):
        adapter = MagicMock()
        adapter.client = _make_adapter_client(db_collections={"petrosa": {}})

        result = await mod.drop_zero_doc_collection(
            adapter, "petrosa", "_write_test_probe", dry_run=True
        )

        assert result.existed is False
        assert result.dropped is False
        assert result.guard_tripped is False

    @pytest.mark.asyncio
    async def test_never_raises_on_existence_check_error(self):
        adapter = MagicMock()
        broken_db = MagicMock()
        broken_db.list_collection_names = AsyncMock(side_effect=Exception("boom"))
        adapter.client = MagicMock(__getitem__=MagicMock(return_value=broken_db))

        result = await mod.drop_zero_doc_collection(
            adapter, "petrosa", "_write_test_probe", dry_run=True
        )

        assert result.dropped is False
        assert result.guard_tripped is True


class TestPurgeStaleBootProbeDocs:
    @pytest.mark.asyncio
    async def test_absent_collection_is_noop(self):
        adapter = MagicMock()
        adapter.client = _make_adapter_client(
            db_collections={"petrosa_data_manager": {}}
        )

        result = await mod.purge_stale_boot_probe_docs(
            adapter, dry_run=True, now=_now()
        )

        assert result.existed is False
        assert result.purged_count == 0

    @pytest.mark.asyncio
    async def test_dry_run_reports_stale_count_without_deleting(self):
        old = (_now() - timedelta(hours=48)).isoformat()
        fresh = (_now() - timedelta(hours=1)).isoformat()
        adapter = MagicMock()
        adapter.client = _make_adapter_client(
            db_collections={
                "petrosa_data_manager": {
                    "tradeengine_boot_probes": [
                        {"created_at": old},
                        {"created_at": old},
                        {"created_at": fresh},
                    ]
                }
            }
        )

        result = await mod.purge_stale_boot_probe_docs(
            adapter, dry_run=True, ttl_hours=24, now=_now()
        )

        assert result.total_doc_count == 3
        assert result.stale_doc_count == 2
        assert result.purged_count == 0

    @pytest.mark.asyncio
    async def test_apply_purges_only_stale_docs(self):
        old = (_now() - timedelta(hours=48)).isoformat()
        fresh = (_now() - timedelta(hours=1)).isoformat()
        docs = [{"created_at": old}, {"created_at": old}, {"created_at": fresh}]
        adapter = MagicMock()
        adapter.client = _make_adapter_client(
            db_collections={"petrosa_data_manager": {"tradeengine_boot_probes": docs}}
        )

        result = await mod.purge_stale_boot_probe_docs(
            adapter, dry_run=False, ttl_hours=24, now=_now()
        )

        assert result.purged_count == 2
        assert len(docs) == 1
        assert docs[0]["created_at"] == fresh

    @pytest.mark.asyncio
    async def test_no_stale_docs_is_noop(self):
        fresh = (_now() - timedelta(hours=1)).isoformat()
        adapter = MagicMock()
        adapter.client = _make_adapter_client(
            db_collections={
                "petrosa_data_manager": {
                    "tradeengine_boot_probes": [{"created_at": fresh}]
                }
            }
        )

        result = await mod.purge_stale_boot_probe_docs(
            adapter, dry_run=False, ttl_hours=24, now=_now()
        )

        assert result.purged_count == 0
        assert result.stale_doc_count == 0

    @pytest.mark.asyncio
    async def test_never_raises_on_count_error(self):
        adapter = MagicMock()
        broken_coll = MagicMock()
        broken_coll.count_documents = AsyncMock(side_effect=Exception("boom"))
        broken_db = MagicMock()
        broken_db.list_collection_names = AsyncMock(
            return_value=["tradeengine_boot_probes"]
        )
        broken_db.__getitem__ = MagicMock(return_value=broken_coll)
        adapter.client = MagicMock(__getitem__=MagicMock(return_value=broken_db))

        result = await mod.purge_stale_boot_probe_docs(
            adapter, dry_run=False, now=_now()
        )

        assert result.existed is True
        assert result.purged_count == 0


class TestExecuteMigration:
    @pytest.mark.asyncio
    async def test_default_targets_only_probes(self):
        adapter = MagicMock()
        adapter.client = _make_adapter_client(
            db_collections={
                "petrosa_data_manager": {
                    "_atlas_quota_probe": [],
                    "_p0_unblock_probe": [],
                },
                "petrosa": {"_write_test_probe": []},
            }
        )

        report = await mod.execute_migration(adapter, dry_run=True)

        assert len(report.probe_results) == 3
        assert report.boot_probe_purge is None
        assert report.boot_probe_stray_copies == []
        assert report.any_guard_tripped() is False

    @pytest.mark.asyncio
    async def test_include_boot_probe_artifacts_opts_in(self):
        adapter = MagicMock()
        adapter.client = _make_adapter_client(
            db_collections={
                "petrosa_data_manager": {
                    "_atlas_quota_probe": [],
                    "_p0_unblock_probe": [],
                    "tradeengine_boot_probes": [],
                },
                "petrosa": {"_write_test_probe": [], "tradeengine_boot_probes": []},
                "mongodb": {"tradeengine_boot_probes": []},
            }
        )

        report = await mod.execute_migration(
            adapter, dry_run=True, include_boot_probe_artifacts=True
        )

        assert report.boot_probe_purge is not None
        assert len(report.boot_probe_stray_copies) == 2

    @pytest.mark.asyncio
    async def test_guard_trip_on_one_target_does_not_block_others(self):
        adapter = MagicMock()
        adapter.client = _make_adapter_client(
            db_collections={
                "petrosa_data_manager": {
                    "_atlas_quota_probe": [{"created_at": "x"}],
                    "_p0_unblock_probe": [],
                },
                "petrosa": {"_write_test_probe": []},
            }
        )

        report = await mod.execute_migration(adapter, dry_run=False)

        by_key = {(r.db_name, r.collection): r for r in report.probe_results}
        assert (
            by_key[("petrosa_data_manager", "_atlas_quota_probe")].guard_tripped is True
        )
        assert by_key[("petrosa_data_manager", "_p0_unblock_probe")].dropped is True
        assert by_key[("petrosa", "_write_test_probe")].dropped is True
        assert report.any_guard_tripped() is True


class TestResolveTtlHours:
    def test_cli_value_wins(self, monkeypatch):
        monkeypatch.setenv("BOOT_PROBE_PURGE_TTL_HOURS", "10")
        assert mod._resolve_ttl_hours(5) == 5

    def test_env_used_when_no_cli_value(self, monkeypatch):
        monkeypatch.setenv("BOOT_PROBE_PURGE_TTL_HOURS", "10")
        assert mod._resolve_ttl_hours(None) == 10

    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("BOOT_PROBE_PURGE_TTL_HOURS", raising=False)
        assert mod._resolve_ttl_hours(None) == mod.DEFAULT_TTL_HOURS

    def test_ignores_non_integer(self, monkeypatch):
        monkeypatch.setenv("BOOT_PROBE_PURGE_TTL_HOURS", "not-a-number")
        assert mod._resolve_ttl_hours(None) == mod.DEFAULT_TTL_HOURS

    def test_clamps_negative_to_zero(self, monkeypatch):
        monkeypatch.delenv("BOOT_PROBE_PURGE_TTL_HOURS", raising=False)
        assert mod._resolve_ttl_hours(-5) == 0

    def test_clamps_cli_negative_to_zero(self, monkeypatch):
        assert mod._resolve_ttl_hours(-1) == 0


class TestBuildArgparser:
    def test_parses_all_flags(self):
        parser = mod._build_argparser()
        args = parser.parse_args(
            ["--apply", "--include-boot-probe-artifacts", "--ttl-hours", "48"]
        )
        assert args.apply is True
        assert args.include_boot_probe_artifacts is True
        assert args.ttl_hours == 48

    def test_requires_mode_flag(self):
        parser = mod._build_argparser()
        with pytest.raises(SystemExit) as ei:
            parser.parse_args([])
        assert ei.value.code == 2

    def test_defaults(self):
        parser = mod._build_argparser()
        args = parser.parse_args(["--dry-run"])
        assert args.include_boot_probe_artifacts is False
        assert args.ttl_hours is None


class TestAmain:
    @pytest.mark.asyncio
    async def test_returns_2_when_mongodb_url_unset(self, monkeypatch):
        monkeypatch.delenv("MONGODB_URL", raising=False)
        rc = await mod._amain(["--dry-run"])
        assert rc == 2

    @pytest.mark.asyncio
    async def test_connects_runs_and_disconnects(self, monkeypatch):
        monkeypatch.setenv("MONGODB_URL", "mongodb://localhost:27017/test")
        fake_adapter = MagicMock()
        fake_adapter.connect = MagicMock()
        fake_adapter.disconnect = MagicMock()

        with (
            patch.object(
                mod, "MongoDBAdapter", return_value=fake_adapter
            ) as adapter_cls,
            patch.object(
                mod,
                "execute_migration",
                new=AsyncMock(return_value=mod.MigrationReport()),
            ) as migrate_mock,
        ):
            rc = await mod._amain(["--dry-run"])

        assert rc == 0
        adapter_cls.assert_called_once_with(
            connection_string="mongodb://localhost:27017/test"
        )
        fake_adapter.connect.assert_called_once()
        fake_adapter.disconnect.assert_called_once()
        migrate_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_3_when_apply_and_guard_tripped(self, monkeypatch):
        monkeypatch.setenv("MONGODB_URL", "mongodb://localhost:27017/test")
        fake_adapter = MagicMock()
        guarded_report = mod.MigrationReport(
            probe_results=[
                mod.ProbeDropResult(
                    db_name="petrosa_data_manager",
                    collection="_atlas_quota_probe",
                    dry_run=False,
                    guard_tripped=True,
                )
            ]
        )

        with (
            patch.object(mod, "MongoDBAdapter", return_value=fake_adapter),
            patch.object(
                mod, "execute_migration", new=AsyncMock(return_value=guarded_report)
            ),
        ):
            rc = await mod._amain(["--apply"])

        assert rc == 3

    @pytest.mark.asyncio
    async def test_logs_boot_probe_summary_when_present(self, monkeypatch):
        monkeypatch.setenv("MONGODB_URL", "mongodb://localhost:27017/test")
        fake_adapter = MagicMock()
        report = mod.MigrationReport(
            boot_probe_purge=mod.BootProbePurgeResult(
                db_name="petrosa_data_manager",
                collection="tradeengine_boot_probes",
                dry_run=True,
                ttl_hours=24,
                stale_doc_count=5,
            ),
            boot_probe_stray_copies=[
                mod.ProbeDropResult(
                    db_name="mongodb",
                    collection="tradeengine_boot_probes",
                    dry_run=True,
                    dropped=False,
                )
            ],
        )

        with (
            patch.object(mod, "MongoDBAdapter", return_value=fake_adapter),
            patch.object(mod, "execute_migration", new=AsyncMock(return_value=report)),
        ):
            rc = await mod._amain(["--dry-run", "--include-boot-probe-artifacts"])

        assert rc == 0

    @pytest.mark.asyncio
    async def test_disconnects_even_when_migration_raises(self, monkeypatch):
        monkeypatch.setenv("MONGODB_URL", "mongodb://localhost:27017/test")
        fake_adapter = MagicMock()

        with (
            patch.object(mod, "MongoDBAdapter", return_value=fake_adapter),
            patch.object(
                mod,
                "execute_migration",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
        ):
            rc = await mod._amain(["--dry-run"])

        assert rc == 4
        fake_adapter.disconnect.assert_called_once()


def test_main_delegates_to_amain(monkeypatch):
    monkeypatch.delenv("MONGODB_URL", raising=False)
    rc = mod.main(["--dry-run"])
    assert rc == 2


def test_default_probe_targets_are_the_three_confirmed_dead_collections():
    assert mod.DEFAULT_PROBE_TARGETS == (
        ("petrosa_data_manager", "_atlas_quota_probe"),
        ("petrosa_data_manager", "_p0_unblock_probe"),
        ("petrosa", "_write_test_probe"),
    )


def test_boot_probe_targets_never_include_binance_db():
    """`binance` DB is retained (leader-election) — never a target here."""
    all_dbs = {db for db, _ in mod.DEFAULT_PROBE_TARGETS}
    all_dbs |= {db for db, _ in mod.BOOT_PROBE_STRAY_COPY_TARGETS}
    all_dbs.add(mod.BOOT_PROBE_PURGE_DB)
    assert "binance" not in all_dbs
