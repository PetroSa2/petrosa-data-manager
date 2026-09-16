"""Tests for the intents TTL-index maintenance job (data-manager#244)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from data_manager.maintenance import intents_ttl_index as itx


def _make_db(index_info_by_collection=None, collection_names=None):
    """Build a fake motor database with AsyncMock collection methods."""
    index_info_by_collection = index_info_by_collection or {}
    db = MagicMock()
    collections: dict = {}

    def getitem(name):
        if name not in collections:
            coll = MagicMock()
            coll.index_information = AsyncMock(
                return_value=index_info_by_collection.get(name, {})
            )
            coll.create_index = AsyncMock(return_value=itx.TTL_INDEX_NAME)
            coll.drop_index = AsyncMock()
            collections[name] = coll
        return collections[name]

    db.__getitem__.side_effect = getitem
    db.command = AsyncMock(return_value={"ok": 1})
    db.list_collection_names = AsyncMock(return_value=collection_names or [])
    db._collections = collections
    return db


def _ttl_meta(field: str, seconds: int) -> dict:
    return {"key": [(field, 1)], "expireAfterSeconds": seconds}


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def test_resolve_database_name_prefers_mongodb_database():
    env = {"MONGODB_DATABASE": "explicit_db", "MONGODB_DB": "legacy_db"}
    assert itx.resolve_database_name(env) == "explicit_db"


def test_resolve_database_name_falls_back_to_mongodb_db():
    env = {"MONGODB_DB": "legacy_db"}
    assert itx.resolve_database_name(env) == "legacy_db"


def test_resolve_database_name_defaults_when_unset():
    assert itx.resolve_database_name({}) == "petrosa_data_manager"
    assert itx.resolve_database_name({}) == itx.DEFAULT_DATABASE


def test_load_config_from_env_parses_ttl_seconds():
    config = itx.load_config_from_env({"MONGODB_INTENTS_TTL_SECONDS": "3600"})
    assert config.ttl_seconds == 3600


def test_load_config_from_env_defaults_and_clamps():
    assert itx.load_config_from_env({}).ttl_seconds == itx.DEFAULT_TTL_SECONDS
    # Below the 60s minimum is clamped up.
    assert (
        itx.load_config_from_env({"MONGODB_INTENTS_TTL_SECONDS": "5"}).ttl_seconds == 60
    )
    # Non-integer falls back to default.
    assert (
        itx.load_config_from_env({"MONGODB_INTENTS_TTL_SECONDS": "nope"}).ttl_seconds
        == itx.DEFAULT_TTL_SECONDS
    )


def test_is_legacy_createdat_ttl_detects_only_createdat_ttl():
    assert itx._is_legacy_createdat_ttl(_ttl_meta("createdAt", 604800)) is True
    # A TTL on received_at is not legacy.
    assert itx._is_legacy_createdat_ttl(_ttl_meta("received_at", 86400)) is False
    # A plain createdAt index (no TTL) is not a legacy TTL index.
    assert itx._is_legacy_createdat_ttl({"key": [("createdAt", 1)]}) is False


def test_index_key_fields_extracts_field_names():
    assert itx._index_key_fields({"key": [("received_at", 1)]}) == ["received_at"]
    assert itx._index_key_fields({"key": [("symbol", 1), ("timestamp", 1)]}) == [
        "symbol",
        "timestamp",
    ]


# --------------------------------------------------------------------------- #
# ensure_intents_ttl_index
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_ensure_creates_index_when_absent():
    db = _make_db(index_info_by_collection={"intents": {"_id_": {"key": [("_id", 1)]}}})
    result = await itx.ensure_intents_ttl_index(db, "petrosa_data_manager")

    assert result.action == "created"
    assert result.database == "petrosa_data_manager"
    assert result.field == "received_at"
    coll = db["intents"]
    coll.create_index.assert_awaited_once()
    _args, kwargs = coll.create_index.call_args
    assert kwargs["name"] == itx.TTL_INDEX_NAME
    assert kwargs["expireAfterSeconds"] == itx.DEFAULT_TTL_SECONDS


@pytest.mark.asyncio
async def test_ensure_noop_when_index_matches_spec():
    db = _make_db(
        index_info_by_collection={
            "intents": {itx.TTL_INDEX_NAME: _ttl_meta("received_at", 86400)}
        }
    )
    result = await itx.ensure_intents_ttl_index(
        db, "petrosa_data_manager", ttl_seconds=86400
    )

    assert result.action == "noop"
    coll = db["intents"]
    coll.create_index.assert_not_awaited()
    coll.drop_index.assert_not_awaited()
    db.command.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_collmod_when_ttl_window_differs():
    db = _make_db(
        index_info_by_collection={
            "intents": {itx.TTL_INDEX_NAME: _ttl_meta("received_at", 604800)}
        }
    )
    result = await itx.ensure_intents_ttl_index(
        db, "petrosa_data_manager", ttl_seconds=86400
    )

    assert result.action == "collmod"
    db.command.assert_awaited_once()
    args, kwargs = db.command.call_args
    assert args[0] == "collMod"
    assert args[1] == "intents"
    assert kwargs["index"]["expireAfterSeconds"] == 86400


@pytest.mark.asyncio
async def test_ensure_drops_legacy_createdat_ttl():
    db = _make_db(
        index_info_by_collection={
            "intents": {
                "createdAt_1": _ttl_meta("createdAt", 604800),
                itx.TTL_INDEX_NAME: _ttl_meta("received_at", 86400),
            }
        }
    )
    result = await itx.ensure_intents_ttl_index(
        db, "petrosa_data_manager", ttl_seconds=86400
    )

    assert "createdAt_1" in result.dropped_legacy
    assert result.action == "noop"  # received_at index already correct
    db["intents"].drop_index.assert_awaited_once_with("createdAt_1")


@pytest.mark.asyncio
async def test_ensure_recreates_when_name_on_wrong_field():
    db = _make_db(
        index_info_by_collection={
            "intents": {itx.TTL_INDEX_NAME: _ttl_meta("timestamp", 86400)}
        }
    )
    result = await itx.ensure_intents_ttl_index(
        db, "petrosa_data_manager", ttl_seconds=86400
    )

    assert result.action == "recreated"
    coll = db["intents"]
    coll.drop_index.assert_awaited_once_with(itx.TTL_INDEX_NAME)
    coll.create_index.assert_awaited_once()


@pytest.mark.asyncio
async def test_dry_run_mutates_nothing_but_reports_action():
    db = _make_db(index_info_by_collection={"intents": {"_id_": {"key": [("_id", 1)]}}})
    result = await itx.ensure_intents_ttl_index(
        db, "petrosa_data_manager", dry_run=True
    )

    assert result.action == "created"
    assert result.dry_run is True
    coll = db["intents"]
    coll.create_index.assert_not_awaited()
    coll.drop_index.assert_not_awaited()
    db.command.assert_not_awaited()


@pytest.mark.asyncio
async def test_dry_run_reports_legacy_drop_without_dropping():
    db = _make_db(
        index_info_by_collection={
            "intents": {"createdAt_1": _ttl_meta("createdAt", 604800)}
        }
    )
    result = await itx.ensure_intents_ttl_index(
        db, "petrosa_data_manager", dry_run=True
    )

    assert result.dropped_legacy == ["createdAt_1"]
    db["intents"].drop_index.assert_not_awaited()


# --------------------------------------------------------------------------- #
# audit_sibling_collections (AC5)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_audit_reports_present_and_absent_siblings():
    db = _make_db(
        index_info_by_collection={
            "pnl_events": {"ttl_x": _ttl_meta("received_at", 86400)},
            "alerts": {"_id_": {"key": [("_id", 1)]}},
        },
        collection_names=["intents", "pnl_events", "alerts"],
    )
    results = await itx.audit_sibling_collections(db, "petrosa_data_manager")

    by_name = {r.collection: r for r in results}
    # Every documented sibling is reported.
    assert set(by_name) == set(itx.SIBLING_COLLECTIONS)
    assert by_name["pnl_events"].present is True
    assert by_name["pnl_events"].ttl_indexes == {"ttl_x": 86400}
    assert by_name["alerts"].present is True
    assert by_name["alerts"].ttl_indexes == {}
    # cio_decisions / execution_events / trades are absent in this fixture.
    assert by_name["trades"].present is False
    # data-manager#271 AC1: `alerts` graduated to the EPHEMERAL override; every
    # other documented sibling keeps the default retain-pending-evidence
    # decision (the `trades` override was removed with data-manager#254).
    assert itx.SIBLING_DECISION_OVERRIDES.keys() == {"alerts"}
    assert by_name["alerts"].decision == itx.SIBLING_DECISION_OVERRIDES["alerts"]
    assert all(
        r.collection not in itx.SIBLING_DECISION_OVERRIDES
        or r.decision == itx.SIBLING_DECISION_OVERRIDES[r.collection]
        for r in results
    )
    assert all(
        r.collection in itx.SIBLING_DECISION_OVERRIDES
        or r.decision == itx.SIBLING_RETENTION_DECISION
        for r in results
    )


# --------------------------------------------------------------------------- #
# ensure_signals_ttl_index (companion to petrosa-bot-ta-analysis#267 AC6)
# --------------------------------------------------------------------------- #


def test_default_signals_ttl_is_seven_days():
    assert itx.DEFAULT_SIGNALS_TTL_SECONDS == 604800


def test_load_config_from_env_parses_signals_ttl_seconds():
    config = itx.load_config_from_env({"MONGODB_SIGNALS_TTL_SECONDS": "3600"})
    assert config.signals_ttl_seconds == 3600


def test_load_config_from_env_defaults_signals_ttl():
    assert (
        itx.load_config_from_env({}).signals_ttl_seconds
        == itx.DEFAULT_SIGNALS_TTL_SECONDS
    )


@pytest.mark.asyncio
async def test_ensure_signals_creates_index_when_absent():
    db = _make_db(index_info_by_collection={"signals": {"_id_": {"key": [("_id", 1)]}}})
    result = await itx.ensure_signals_ttl_index(db, "petrosa_data_manager")

    assert result.action == "created"
    assert result.database == "petrosa_data_manager"
    assert result.collection == "signals"
    assert result.field == "_ttl_inserted_at"
    coll = db["signals"]
    coll.create_index.assert_awaited_once()
    _args, kwargs = coll.create_index.call_args
    assert kwargs["name"] == itx.SIGNALS_TTL_INDEX_NAME
    assert kwargs["expireAfterSeconds"] == itx.DEFAULT_SIGNALS_TTL_SECONDS


@pytest.mark.asyncio
async def test_ensure_signals_noop_when_index_matches_spec():
    db = _make_db(
        index_info_by_collection={
            "signals": {
                itx.SIGNALS_TTL_INDEX_NAME: _ttl_meta("_ttl_inserted_at", 604800)
            }
        }
    )
    result = await itx.ensure_signals_ttl_index(
        db, "petrosa_data_manager", ttl_seconds=604800
    )

    assert result.action == "noop"
    coll = db["signals"]
    coll.create_index.assert_not_awaited()
    coll.drop_index.assert_not_awaited()
    db.command.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_signals_collmod_when_ttl_window_differs():
    db = _make_db(
        index_info_by_collection={
            "signals": {
                itx.SIGNALS_TTL_INDEX_NAME: _ttl_meta("_ttl_inserted_at", 604800)
            }
        }
    )
    result = await itx.ensure_signals_ttl_index(
        db, "petrosa_data_manager", ttl_seconds=86400
    )

    assert result.action == "collmod"
    db.command.assert_awaited_once()
    args, kwargs = db.command.call_args
    assert args[0] == "collMod"
    assert args[1] == "signals"
    assert kwargs["index"]["expireAfterSeconds"] == 86400


@pytest.mark.asyncio
async def test_ensure_signals_recreates_when_name_on_wrong_field():
    db = _make_db(
        index_info_by_collection={
            "signals": {itx.SIGNALS_TTL_INDEX_NAME: _ttl_meta("timestamp", 604800)}
        }
    )
    result = await itx.ensure_signals_ttl_index(db, "petrosa_data_manager")

    assert result.action == "recreated"
    coll = db["signals"]
    coll.drop_index.assert_awaited_once_with(itx.SIGNALS_TTL_INDEX_NAME)
    coll.create_index.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_signals_dry_run_mutates_nothing():
    db = _make_db(index_info_by_collection={"signals": {"_id_": {"key": [("_id", 1)]}}})
    result = await itx.ensure_signals_ttl_index(
        db, "petrosa_data_manager", dry_run=True
    )

    assert result.action == "created"
    assert result.dry_run is True
    coll = db["signals"]
    coll.create_index.assert_not_awaited()
    coll.drop_index.assert_not_awaited()
    db.command.assert_not_awaited()


# --------------------------------------------------------------------------- #
# ensure_alerts_ttl_index (data-manager#271 AC6)
# --------------------------------------------------------------------------- #


def test_default_alerts_ttl_is_seven_days():
    assert itx.DEFAULT_ALERTS_TTL_SECONDS == 604800
    assert itx.ALERTS_COLLECTION == "alerts"
    assert itx.ALERTS_TTL_FIELD == "_ttl_inserted_at"
    assert itx.ALERTS_TTL_INDEX_NAME == "_ttl_inserted_at_ttl"


def test_load_config_from_env_parses_alerts_ttl_seconds():
    config = itx.load_config_from_env({"MONGODB_ALERTS_TTL_SECONDS": "3600"})
    assert config.alerts_ttl_seconds == 3600


def test_load_config_from_env_defaults_alerts_ttl():
    assert (
        itx.load_config_from_env({}).alerts_ttl_seconds
        == itx.DEFAULT_ALERTS_TTL_SECONDS
    )


@pytest.mark.asyncio
async def test_ensure_alerts_creates_index_when_absent():
    db = _make_db(index_info_by_collection={"alerts": {"_id_": {"key": [("_id", 1)]}}})
    result = await itx.ensure_alerts_ttl_index(db, "petrosa_data_manager")

    assert result.action == "created"
    assert result.database == "petrosa_data_manager"
    assert result.collection == "alerts"
    assert result.field == "_ttl_inserted_at"
    coll = db["alerts"]
    coll.create_index.assert_awaited_once()
    _args, kwargs = coll.create_index.call_args
    assert kwargs["name"] == itx.ALERTS_TTL_INDEX_NAME
    assert kwargs["expireAfterSeconds"] == itx.DEFAULT_ALERTS_TTL_SECONDS


@pytest.mark.asyncio
async def test_ensure_alerts_noop_when_index_matches_spec():
    db = _make_db(
        index_info_by_collection={
            "alerts": {itx.ALERTS_TTL_INDEX_NAME: _ttl_meta("_ttl_inserted_at", 604800)}
        }
    )
    result = await itx.ensure_alerts_ttl_index(
        db, "petrosa_data_manager", ttl_seconds=604800
    )

    assert result.action == "noop"
    coll = db["alerts"]
    coll.create_index.assert_not_awaited()
    coll.drop_index.assert_not_awaited()
    db.command.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_alerts_collmod_when_ttl_window_differs():
    db = _make_db(
        index_info_by_collection={
            "alerts": {itx.ALERTS_TTL_INDEX_NAME: _ttl_meta("_ttl_inserted_at", 604800)}
        }
    )
    result = await itx.ensure_alerts_ttl_index(
        db, "petrosa_data_manager", ttl_seconds=86400
    )

    assert result.action == "collmod"
    db.command.assert_awaited_once()
    args, kwargs = db.command.call_args
    assert args[0] == "collMod"
    assert args[1] == "alerts"
    assert kwargs["index"]["expireAfterSeconds"] == 86400


@pytest.mark.asyncio
async def test_ensure_alerts_recreates_when_name_on_wrong_field():
    db = _make_db(
        index_info_by_collection={
            "alerts": {itx.ALERTS_TTL_INDEX_NAME: _ttl_meta("timestamp", 604800)}
        }
    )
    result = await itx.ensure_alerts_ttl_index(db, "petrosa_data_manager")

    assert result.action == "recreated"
    coll = db["alerts"]
    coll.drop_index.assert_awaited_once_with(itx.ALERTS_TTL_INDEX_NAME)
    coll.create_index.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_alerts_dry_run_mutates_nothing():
    db = _make_db(index_info_by_collection={"alerts": {"_id_": {"key": [("_id", 1)]}}})
    result = await itx.ensure_alerts_ttl_index(db, "petrosa_data_manager", dry_run=True)

    assert result.action == "created"
    assert result.dry_run is True
    coll = db["alerts"]
    coll.create_index.assert_not_awaited()
    coll.drop_index.assert_not_awaited()


# --------------------------------------------------------------------------- #
# ensure_config_rate_limits_ttl_index (data-manager#302)
# --------------------------------------------------------------------------- #


def test_default_config_rate_limits_ttl_is_one_hour():
    assert itx.DEFAULT_CONFIG_RATE_LIMITS_TTL_SECONDS == 3600
    assert itx.CONFIG_RATE_LIMITS_COLLECTION == "config_rate_limits"
    assert itx.CONFIG_RATE_LIMITS_TTL_FIELD == "timestamp"
    assert itx.CONFIG_RATE_LIMITS_TTL_INDEX_NAME == "timestamp_ttl_1h"
    assert itx.DEFAULT_CONFIG_RATE_LIMITS_DATABASE == "petrosa"


def test_load_config_from_env_parses_config_rate_limits_ttl_seconds():
    config = itx.load_config_from_env(
        {"MONGODB_CONFIG_RATE_LIMITS_TTL_SECONDS": "7200"}
    )
    assert config.config_rate_limits_ttl_seconds == 7200


def test_load_config_from_env_defaults_config_rate_limits_ttl_and_database():
    config = itx.load_config_from_env({})
    assert (
        config.config_rate_limits_ttl_seconds
        == itx.DEFAULT_CONFIG_RATE_LIMITS_TTL_SECONDS
    )
    assert config.config_rate_limits_database == itx.DEFAULT_CONFIG_RATE_LIMITS_DATABASE


def test_load_config_from_env_overrides_config_rate_limits_database():
    config = itx.load_config_from_env(
        {"MONGODB_CONFIG_RATE_LIMITS_DATABASE": "custom_db"}
    )
    assert config.config_rate_limits_database == "custom_db"


@pytest.mark.asyncio
async def test_ensure_config_rate_limits_creates_index_when_absent():
    db = _make_db(
        index_info_by_collection={"config_rate_limits": {"_id_": {"key": [("_id", 1)]}}}
    )
    result = await itx.ensure_config_rate_limits_ttl_index(db, "petrosa")

    assert result.action == "created"
    assert result.database == "petrosa"
    assert result.collection == "config_rate_limits"
    assert result.field == "timestamp"
    coll = db["config_rate_limits"]
    coll.create_index.assert_awaited_once()
    _args, kwargs = coll.create_index.call_args
    assert kwargs["name"] == itx.CONFIG_RATE_LIMITS_TTL_INDEX_NAME
    assert kwargs["expireAfterSeconds"] == itx.DEFAULT_CONFIG_RATE_LIMITS_TTL_SECONDS


@pytest.mark.asyncio
async def test_ensure_config_rate_limits_noop_when_index_matches_spec():
    db = _make_db(
        index_info_by_collection={
            "config_rate_limits": {
                itx.CONFIG_RATE_LIMITS_TTL_INDEX_NAME: _ttl_meta("timestamp", 3600)
            }
        }
    )
    result = await itx.ensure_config_rate_limits_ttl_index(
        db, "petrosa", ttl_seconds=3600
    )

    assert result.action == "noop"
    coll = db["config_rate_limits"]
    coll.create_index.assert_not_awaited()
    coll.drop_index.assert_not_awaited()
    db.command.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_config_rate_limits_collmod_when_ttl_window_differs():
    db = _make_db(
        index_info_by_collection={
            "config_rate_limits": {
                itx.CONFIG_RATE_LIMITS_TTL_INDEX_NAME: _ttl_meta("timestamp", 3600)
            }
        }
    )
    result = await itx.ensure_config_rate_limits_ttl_index(
        db, "petrosa", ttl_seconds=7200
    )

    assert result.action == "collmod"
    db.command.assert_awaited_once()
    args, kwargs = db.command.call_args
    assert args[0] == "collMod"
    assert args[1] == "config_rate_limits"
    assert kwargs["index"]["expireAfterSeconds"] == 7200


@pytest.mark.asyncio
async def test_ensure_config_rate_limits_recreates_when_name_on_wrong_field():
    db = _make_db(
        index_info_by_collection={
            "config_rate_limits": {
                itx.CONFIG_RATE_LIMITS_TTL_INDEX_NAME: _ttl_meta("changed_by", 3600)
            }
        }
    )
    result = await itx.ensure_config_rate_limits_ttl_index(db, "petrosa")

    assert result.action == "recreated"
    coll = db["config_rate_limits"]
    coll.drop_index.assert_awaited_once_with(itx.CONFIG_RATE_LIMITS_TTL_INDEX_NAME)
    coll.create_index.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_config_rate_limits_dry_run_mutates_nothing():
    db = _make_db(
        index_info_by_collection={"config_rate_limits": {"_id_": {"key": [("_id", 1)]}}}
    )
    result = await itx.ensure_config_rate_limits_ttl_index(db, "petrosa", dry_run=True)

    assert result.action == "created"
    assert result.dry_run is True
    coll = db["config_rate_limits"]
    coll.create_index.assert_not_awaited()
    coll.drop_index.assert_not_awaited()


# --------------------------------------------------------------------------- #
# _amain wiring for config_rate_limits (data-manager#302)
# --------------------------------------------------------------------------- #


class TestAmainConfigRateLimits:
    """`config_rate_limits` lives in a different (shared) database than
    intents/signals/alerts — verify `_amain` selects it explicitly and
    respects `--skip-config-rate-limits`."""

    def _fake_adapter(self):
        adapter = MagicMock()
        adapter.connect = MagicMock()
        adapter.disconnect = MagicMock()
        databases: dict = {}

        def getitem(name):
            if name not in databases:
                databases[name] = _make_db()
            return databases[name]

        adapter.client.__getitem__.side_effect = getitem
        adapter.client._databases = databases
        return adapter

    @pytest.mark.asyncio
    async def test_amain_ensures_config_rate_limits_on_shared_petrosa_db(
        self, monkeypatch
    ):
        monkeypatch.setenv("MONGODB_URL", "mongodb://localhost:27017/test")
        monkeypatch.setenv("MONGODB_DATABASE", "petrosa_data_manager")
        fake_adapter = self._fake_adapter()

        with (
            patch.object(itx, "MongoDBAdapter", return_value=fake_adapter),
            patch.object(
                itx,
                "ensure_config_rate_limits_ttl_index",
                new=AsyncMock(wraps=itx.ensure_config_rate_limits_ttl_index),
            ) as ensure_mock,
        ):
            rc = await itx._amain(["--dry-run"])

        assert rc == 0
        ensure_mock.assert_awaited_once()
        args, _kwargs = ensure_mock.call_args
        # First positional arg is the `db` handle selected from the shared
        # "petrosa" database, second is that database's name — NOT this
        # repo's own "petrosa_data_manager".
        assert args[1] == "petrosa"

    @pytest.mark.asyncio
    async def test_amain_skips_config_rate_limits_when_flag_passed(self, monkeypatch):
        monkeypatch.setenv("MONGODB_URL", "mongodb://localhost:27017/test")
        fake_adapter = self._fake_adapter()

        with (
            patch.object(itx, "MongoDBAdapter", return_value=fake_adapter),
            patch.object(
                itx, "ensure_config_rate_limits_ttl_index", new=AsyncMock()
            ) as ensure_mock,
        ):
            rc = await itx._amain(["--dry-run", "--skip-config-rate-limits"])

        assert rc == 0
        ensure_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_amain_honors_config_rate_limits_database_override(self, monkeypatch):
        monkeypatch.setenv("MONGODB_URL", "mongodb://localhost:27017/test")
        fake_adapter = self._fake_adapter()

        with (
            patch.object(itx, "MongoDBAdapter", return_value=fake_adapter),
            patch.object(
                itx, "ensure_config_rate_limits_ttl_index", new=AsyncMock()
            ) as ensure_mock,
        ):
            rc = await itx._amain(
                [
                    "--dry-run",
                    "--config-rate-limits-database",
                    "custom_shared_db",
                ]
            )

        assert rc == 0
        args, _kwargs = ensure_mock.call_args
        assert args[1] == "custom_shared_db"
