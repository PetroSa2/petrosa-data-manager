from __future__ import annotations

from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from data_manager.maintenance import repair_positions_auto_increment as mod


def _engine() -> sa.Engine:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE TABLE positions (id BIGINT PRIMARY KEY, symbol TEXT, quantity INTEGER)"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO positions (id, symbol, quantity) VALUES (:id, :symbol, :quantity)"
            ),
            [
                {"id": 1, "symbol": "BTCUSDT", "quantity": 2},
                {"id": 5, "symbol": "ETHUSDT", "quantity": 3},
                {"id": 66670200000000, "symbol": "SOLUSDT", "quantity": 4},
                {"id": 9223372036854775807, "symbol": "XRPUSDT", "quantity": 5},
            ],
        )
    return engine


def test_dry_run_plans_rekeys_without_writes() -> None:
    engine = _engine()
    result = mod.execute_repair(engine, dry_run=True)
    assert result["updates"] == [
        {"old_id": 66670200000000, "new_id": 6},
        {"old_id": 9223372036854775807, "new_id": 7},
    ]
    assert result["final_auto_increment"] == 8
    with engine.connect() as connection:
        assert connection.execute(
            sa.text("SELECT id FROM positions ORDER BY id")
        ).scalars().all() == [
            1,
            5,
            66670200000000,
            9223372036854775807,
        ]


def test_apply_rekeys_and_preserves_other_columns() -> None:
    engine = _engine()
    before = _rows(engine)
    result = mod.execute_repair(engine, dry_run=False)
    assert result["applied"] is True
    after = _rows(engine)
    assert [row[0] for row in after] == [1, 5, 6, 7]
    assert sorted(row[1:] for row in after) == sorted(row[1:] for row in before)


def test_foreign_key_reference_aborts_before_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine()
    monkeypatch.setattr(
        mod, "foreign_key_references", lambda _engine: [{"table_name": "fills"}]
    )
    with pytest.raises(mod.RepairSafetyError):
        mod.execute_repair(engine, dry_run=False)
    assert _rows(engine)[-2][0] == 66670200000000


def test_apply_requires_both_confirmations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mod, "_make_engine_from_env", lambda: pytest.fail("must not connect")
    )
    assert mod.main(["--apply"]) != 0
    assert mod.main(["--apply", "--confirm-table", "positions"]) != 0


def test_main_requires_database_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MYSQL_URI", raising=False)
    assert mod.main(["--dry-run"]) == 2


def test_main_dry_run_uses_engine_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _engine()
    monkeypatch.setattr(mod, "_make_engine_from_env", lambda: engine)
    assert mod.main(["--dry-run"]) == 0


def test_main_rejects_confirmation_in_dry_run() -> None:
    with pytest.raises(SystemExit) as error:
        mod.main(["--dry-run", "--confirm-table", "positions"])
    assert error.value.code == 2


def test_sqlite_foreign_key_is_detected() -> None:
    engine = _engine()
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE TABLE fills (id INTEGER PRIMARY KEY, position_id BIGINT "
                "REFERENCES positions(id))"
            )
        )
    references = mod.foreign_key_references(engine)
    assert references[0]["table_name"] == "fills"


def test_mysql_metadata_and_ddl_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = _FakeConnection(
        [
            {"auto_increment": 8},
            {
                "TABLE_NAME": "fills",
                "CONSTRAINT_NAME": "fk_fill_position",
                "COLUMN_NAME": "position_id",
            },
        ]
    )
    engine = SimpleNamespace(dialect=SimpleNamespace(name="mysql"))
    monkeypatch.setattr(
        engine, "connect", lambda: _ConnectionContext(connection), raising=False
    )
    monkeypatch.setattr(
        engine, "begin", lambda: _ConnectionContext(connection), raising=False
    )

    assert mod.read_auto_increment(engine) == 8
    assert mod.foreign_key_references(engine) == [
        {
            "TABLE_NAME": "fills",
            "CONSTRAINT_NAME": "fk_fill_position",
            "COLUMN_NAME": "position_id",
        }
    ]
    mod.set_auto_increment(engine, 8)
    assert "ALTER TABLE positions AUTO_INCREMENT = 8" in connection.statements[-1]


def test_row_value_falls_back_to_sequence() -> None:
    assert mod._row_value((8,), "missing") == 8


def _rows(engine: sa.Engine) -> list[tuple[object, ...]]:
    with engine.connect() as connection:
        return [
            tuple(row)
            for row in connection.execute(
                sa.text("SELECT * FROM positions ORDER BY id")
            )
        ]


class _FakeConnection:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.statements: list[str] = []

    def execute(
        self, statement: object, *_args: object, **_kwargs: object
    ) -> _FakeResult:
        self.statements.append(str(statement))
        if "AUTO_INCREMENT" in str(statement):
            return _FakeResult(self.rows[:1])
        return _FakeResult(self.rows[1:])


class _FakeResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = [SimpleNamespace(_mapping=row) for row in rows]

    def fetchone(self) -> SimpleNamespace | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[SimpleNamespace]:
        return self.rows


class _ConnectionContext:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection

    def __enter__(self) -> _FakeConnection:
        return self.connection

    def __exit__(self, *_args: object) -> None:
        return None
