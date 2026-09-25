from __future__ import annotations

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


def _rows(engine: sa.Engine) -> list[tuple[object, ...]]:
    with engine.connect() as connection:
        return [
            tuple(row)
            for row in connection.execute(
                sa.text("SELECT * FROM positions ORDER BY id")
            )
        ]
