import unicodedata
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from mongomock_motor import AsyncMongoMockClient
from sqlalchemy.dialects import mysql
from sqlalchemy.sql.ddl import SchemaGenerator
from sqlalchemy.sql.dml import Insert, Update
from sqlalchemy.sql.selectable import Select

from data_manager.api.routes import trading_state as module
from data_manager.db.base_adapter import DatabaseError
from data_manager.db.mysql_adapter import MySQLAdapter
from data_manager.db.repositories.trading_state_repository import TradingStateRepository


def adapter():
    value = MySQLAdapter("mysql+pymysql://unused/unused", role="serving")
    value._create_tables()
    value.tables["positions"] = sa.Table(
        "positions",
        value.metadata,
        sa.Column("position_id", sa.String(64), primary_key=True),
        sa.Column("status", sa.String(32)),
        sa.Column("quantity", sa.Numeric(20, 8)),
        sa.Column("close_reason", sa.String(128)),
        sa.Column("pnl_unknown", sa.Integer),
    )
    value._connected = True
    return value


class SchemaFixture:
    def __init__(self, rows):
        self.rows = deepcopy(rows)
        self.sql = []
        self.dialect = mysql.dialect()
        self.dialect.server_version_info = (5, 7, 44)
        self.dialect.has_table = lambda _, name, schema=None: name in self.rows

    def schema_for_object(self, obj):
        return obj.schema

    def execute(self, stmt):
        self.sql.append(str(stmt.compile(dialect=self.dialect)))
        if isinstance(stmt, sa.schema.CreateTable):
            assert stmt.element.name not in self.rows
            self.rows[stmt.element.name] = []

    def _run_ddl_visitor(self, visitor, element, **kwargs):
        visitor(self.dialect, self, **kwargs).traverse_single(element)


@pytest.mark.parametrize("upgrade", [False, True])
def test_bootstrap_twice_retains_rows_and_creates_missing_schema(upgrade):
    value = adapter()
    existing = {"positions": [{"position_id": "historic", "status": "closed"}]}
    if upgrade:
        existing["trading_state_mysql_dead_letters"] = [
            {"id": 123, "payload": {"retained": True}}
        ]
    fixture = SchemaFixture(existing)
    value.metadata.create_all(fixture)
    first = list(fixture.sql)
    value.metadata.create_all(fixture)
    assert fixture.sql == first
    for name, rows in existing.items():
        assert fixture.rows[name] == rows
    assert "trading_state_mysql_dead_letters" in fixture.rows
    assert "trading_state_mysql_versions" in fixture.rows
    emitted = "\n".join(fixture.sql)
    assert "trading_state_mysql_versions" in emitted
    assert "ENGINE=InnoDB" in emitted
    assert "ALTER TABLE" not in emitted
    assert "DROP" not in emitted
    assert "AUTO_INCREMENT" in str(
        sa.schema.CreateTable(value.tables["trading_state_mysql_dead_letters"]).compile(
            dialect=fixture.dialect
        )
    )


def test_operator_migration_is_only_idempotent_additive_mysql57_ddl():
    sql = Path(
        "data_manager/scripts/migrations/019_trading_state_recovery.sql"
    ).read_text()
    statements = [part.strip() for part in sql.split(";") if part.strip()]
    assert len(statements) == 2
    assert all(
        part.startswith("CREATE TABLE IF NOT EXISTS trading_state_mysql_")
        for part in statements
    )
    assert all(part.endswith("ENGINE=InnoDB") for part in statements)
    assert "payload JSON NOT NULL" in sql
    assert "DATETIME(6)" in sql
    assert "ADD COLUMN IF NOT EXISTS" not in sql
    assert "DROP" not in sql
    value = adapter()
    dead_letters = value.tables["trading_state_mysql_dead_letters"]
    versions = value.tables["trading_state_mysql_versions"]
    assert set(dead_letters.c.keys()) == {
        "id",
        "operation",
        "reason",
        "payload",
        "created_at",
    }
    assert set(versions.c.keys()) == {"copy_key", "version"}
    assert versions.c.copy_key.primary_key
    assert not versions.c.version.nullable


class TransactionFixture:
    def __init__(self):
        self.committed = {
            "trading_state_mysql_versions": {},
            "positions": {},
            "daily_pnl": {},
        }
        self.statements = []
        self.fail_cursor_update = False
        self.effects = 0
        self.case_insensitive = False

    def matching_key(self, rows, key):
        if not self.case_insensitive:
            return key

        def equivalent(value):
            return "".join(
                character
                for character in unicodedata.normalize("NFD", value.casefold().rstrip())
                if not unicodedata.combining(character)
            )

        return next(
            (stored for stored in rows if equivalent(stored) == equivalent(key)), key
        )

    def begin(self):
        return self

    def __enter__(self):
        self.pending = deepcopy(self.committed)
        return self

    def __exit__(self, kind, value, traceback):
        if kind is None:
            self.committed = self.pending
        return False

    def execute(self, stmt):
        compiled = stmt.compile(dialect=mysql.dialect())
        sql = str(compiled)
        self.statements.append(sql)
        params = compiled.params
        name = (
            stmt.get_final_froms()[0].name
            if isinstance(stmt, Select)
            else stmt.table.name
        )
        rows = self.pending[name]
        if isinstance(stmt, Insert):
            key = (
                params.get("copy_key")
                if name.endswith("versions")
                else params.get("position_id", params.get("date"))
            )
            if name.endswith("versions"):
                assert "ON DUPLICATE KEY UPDATE" in sql
                rows.setdefault(key, params)
            else:
                assert key not in rows
                rows[key] = params
                self.effects += 1
            return SimpleNamespace(rowcount=1)
        key = self.matching_key(rows, stmt._where_criteria[0].right.value)
        if isinstance(stmt, Select):
            if name.endswith("versions"):
                assert sql.endswith("FOR UPDATE")
                return SimpleNamespace(scalar_one=lambda: rows[key]["version"])
            result = SimpleNamespace(first=lambda: rows.get(key))
            result.mappings = lambda: result
            return result
        assert isinstance(stmt, Update)
        if name.endswith("versions") and self.fail_cursor_update:
            raise RuntimeError("lost connection before commit")
        rows[key].update({field: expr.value for field, expr in stmt._values.items()})
        if not name.endswith("versions"):
            self.effects += 1
        return SimpleNamespace(rowcount=1)


def test_real_adapter_transaction_prevents_stale_and_duplicate_logical_effects():
    value = adapter()
    engine = TransactionFixture()
    value.engine = engine
    value.persist_trading_state(
        "positions", {"position_id": "p1", "status": "open", "quantity": 1}, 1
    )
    value.persist_trading_state(
        "positions", {"position_id": "p1", "status": "closed", "quantity": 0}, 2
    )
    value.persist_trading_state(
        "positions", {"position_id": "p1", "status": "open", "quantity": 1}, 1
    )
    value.persist_trading_state(
        "positions", {"position_id": "p1", "status": "closed", "quantity": 0}, 2
    )
    assert engine.effects == 2
    assert engine.committed["positions"]["p1"]["status"] == "closed"
    assert engine.committed["positions"]["p1"]["quantity"] == 0
    assert (
        list(engine.committed["trading_state_mysql_versions"].values())[0]["version"]
        == 2
    )
    assert sum("FOR UPDATE" in sql for sql in engine.statements) == 8


def test_target_and_cursor_rollback_together_then_resume():
    value = adapter()
    engine = TransactionFixture()
    value.engine = engine
    engine.fail_cursor_update = True
    with pytest.raises(RuntimeError, match="before commit"):
        value.persist_trading_state(
            "positions", {"position_id": "p1", "status": "open"}, 1
        )
    assert engine.committed["positions"] == {}
    assert engine.committed["trading_state_mysql_versions"] == {}
    engine.fail_cursor_update = False
    value.persist_trading_state(
        "positions", {"position_id": "p1", "status": "closed"}, 2
    )
    assert engine.committed["positions"]["p1"]["status"] == "closed"
    assert (
        list(engine.committed["trading_state_mysql_versions"].values())[0]["version"]
        == 2
    )


def test_daily_pnl_insert_has_required_values_and_repeat_is_idempotent():
    value = adapter()
    engine = TransactionFixture()
    value.engine = engine
    value.persist_trading_state("daily_pnl", {"date": "2026-10-22", "daily_pnl": 3}, 1)
    row = engine.committed["daily_pnl"]["2026-10-22"]
    assert row["id"]
    assert row["created_at"]
    assert row["updated_at"]
    value.persist_trading_state("daily_pnl", {"date": "2026-10-22", "daily_pnl": 3}, 1)
    assert engine.effects == 1


def test_quantity_replay_preserves_ledger_supersede_fields():
    value = adapter()
    engine = TransactionFixture()
    value.engine = engine
    value.persist_trading_state(
        "positions",
        {
            "position_id": "p1",
            "status": "open",
            "quantity": 1,
            "close_reason": None,
            "pnl_unknown": 0,
        },
        1,
    )
    engine.committed["positions"]["p1"].update(
        status="superseded",
        close_reason="phantom_superseded",
        pnl_unknown=1,
    )
    value.persist_trading_state(
        "positions",
        {
            "position_id": "p1",
            "status": "open",
            "quantity": 2,
            "close_reason": None,
            "pnl_unknown": 0,
        },
        2,
    )
    row = engine.committed["positions"]["p1"]
    assert row["quantity"] == 2
    assert row["status"] == "superseded"
    assert row["close_reason"] == "phantom_superseded"
    assert row["pnl_unknown"] == 1


@pytest.mark.parametrize(
    "stored,alias", [("P1", "p1"), ("caf\u00e9", "cafe"), ("p1", "p1 ")]
)
def test_sql_equivalent_id_is_rejected_before_cursor_ack(stored, alias):
    value = adapter()
    engine = TransactionFixture()
    engine.case_insensitive = True
    value.engine = engine
    value.persist_trading_state(
        "positions", {"position_id": stored, "status": "closed"}, 2
    )
    before = deepcopy(engine.committed)
    with pytest.raises(DatabaseError, match="identity") as error:
        value.persist_trading_state(
            "positions", {"position_id": alias, "status": "open"}, 1
        )
    assert "identity" in str(error.value)
    assert engine.committed == before


@pytest.mark.asyncio
async def test_typed_replay_recreation_preserves_version_and_ledger_ownership(
    monkeypatch,
):
    mysql_adapter = adapter()
    engine = TransactionFixture()
    mysql_adapter.engine = engine
    mongo = SimpleNamespace(
        db=AsyncMongoMockClient().recovery, _prepare_for_bson=lambda value: value
    )
    monkeypatch.setattr(
        module.api_module,
        "db_manager",
        SimpleNamespace(
            mysql_adapter=mysql_adapter,
            mongodb_adapter=mongo,
        ),
    )
    monkeypatch.setattr(module, "_schedule_mysql_copy", lambda: None)
    await module.create_position(
        module.TradingDocument(position_id="p1", status="open", quantity=1)
    )
    await module._replay_mysql_copies()
    engine.committed["positions"]["p1"].update(
        status="superseded", close_reason="ledger", pnl_unknown=1
    )
    await module.patch_position("p1", module.TradingDocument(quantity=2))
    await module._replay_mysql_copies()
    assert engine.committed["positions"]["p1"]["status"] == "superseded"
    await mongo.db.positions.delete_one({"position_id": "p1"})
    await module.create_position(
        module.TradingDocument(position_id="p1", status="open", quantity=99)
    )
    await module._replay_mysql_copies()
    row = engine.committed["positions"]["p1"]
    assert row["quantity"] == 99
    assert row["status"] == "superseded"
    assert row["close_reason"] == "ledger"
    assert row["pnl_unknown"] == 1
    assert await TradingStateRepository(mysql_adapter, mongo).pending_copy_count() == 0


@pytest.mark.asyncio
async def test_sql_equivalent_alias_remains_durable_pending(monkeypatch):
    mysql_adapter = adapter()
    engine = TransactionFixture()
    engine.case_insensitive = True
    mysql_adapter.engine = engine
    mongo = SimpleNamespace(
        db=AsyncMongoMockClient().recovery, _prepare_for_bson=lambda value: value
    )
    monkeypatch.setattr(
        module.api_module,
        "db_manager",
        SimpleNamespace(mysql_adapter=mysql_adapter, mongodb_adapter=mongo),
    )
    monkeypatch.setattr(module, "_schedule_mysql_copy", lambda: None)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    mysql_adapter.persist_trading_state(
        "positions", {"position_id": "P1", "status": "closed"}, 2
    )
    await module.create_position(
        module.TradingDocument(position_id="p1", status="open")
    )
    await module._replay_mysql_copies()
    assert engine.committed["positions"]["P1"]["status"] == "closed"
    pending = await TradingStateRepository(mysql_adapter, mongo).pending_copies(
        "positions", 5
    )
    assert len(pending) == 1
    assert pending[0]["position_id"] == "p1"


def test_sql_primary_identity_cannot_change():
    value = adapter()
    value.tables["positions"].append_column(sa.Column("id", sa.Integer))
    engine = TransactionFixture()
    value.engine = engine
    value.persist_trading_state(
        "positions", {"position_id": "p1", "id": 1, "status": "open"}, 1
    )
    before = deepcopy(engine.committed)
    with pytest.raises(DatabaseError, match="identity") as error:
        value.persist_trading_state(
            "positions", {"position_id": "p1", "id": 2, "status": "closed"}, 2
        )
    assert "identity" in str(error.value)
    assert engine.committed == before


def test_missing_committed_target_is_not_a_successful_noop():
    value = adapter()
    engine = TransactionFixture()
    value.engine = engine
    value.persist_trading_state("positions", {"position_id": "p1", "status": "open"}, 1)
    engine.committed["positions"].clear()
    with pytest.raises(DatabaseError, match="target is missing") as error:
        value.persist_trading_state(
            "positions", {"position_id": "p1", "status": "open"}, 1
        )
    assert "missing" in str(error.value)
