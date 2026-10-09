from __future__ import annotations

import ast
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from prometheus_client import REGISTRY
from sqlalchemy import event

from data_manager.db.engine_factory import (
    ERROR_KINDS,
    ROLE_DEFAULTS,
    EngineClosingError,
    build_engine,
    classify_connection_error,
    mark_engine_closing,
    role_options,
)
from data_manager.db.mysql_adapter import MySQLAdapter

SQLITE = "sqlite+pysqlite:///:memory:"
MYSQL = "mysql+pymysql://user:pass@127.0.0.1:3306/db"


def _sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _errors() -> dict[str, float]:
    return {
        kind: _sample("data_manager_mysql_connection_errors_total", kind=kind)
        for kind in ERROR_KINDS
    }


def _delta(before: dict[str, float]) -> dict[str, float]:
    after = _errors()
    return {kind: after[kind] - before[kind] for kind in ERROR_KINDS}


def _failing_connect(engine, message: str) -> list[int]:
    """Make every DBAPI connect fail with ``message``; the list counts attempts."""
    attempts: list[int] = []

    @event.listens_for(engine, "do_connect")
    def _fail(dialect, conn_rec, cargs, cparams):
        attempts.append(1)
        raise sqlite3.OperationalError(message)

    return attempts


def test_role_defaults_and_suffix_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MYSQL_POOL_SIZE_CRON", "3")
    monkeypatch.setenv("MYSQL_MAX_OVERFLOW_CRON", "2")
    monkeypatch.setenv("MYSQL_POOL_TIMEOUT_CRON", "4")
    assert role_options("cron") == {
        "pool_size": 3,
        "max_overflow": 2,
        "pool_timeout": 4,
    }


def test_invalid_role_env_uses_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MYSQL_POOL_SIZE_ADHOC", "0")
    monkeypatch.setenv("MYSQL_POOL_TIMEOUT_ADHOC", "invalid")
    assert role_options("adhoc") == {
        "pool_size": 1,
        "max_overflow": 1,
        "pool_timeout": 5,
    }


def test_closing_engine_is_not_a_budget_error() -> None:
    """The "engine is closing" rejection is its own class and is counted nowhere.

    Its text used to contain "QueuePool", so shutdown races were counted as
    pool timeouts and inflated the counter the budget alert reads.
    """
    errors: list[str] = []
    engine = build_engine(SQLITE, "adhoc", on_error=errors.append)
    mark_engine_closing(engine)
    before = _errors()
    with pytest.raises(EngineClosingError, match="closing"):
        engine.connect()
    assert errors == []
    assert _delta(before) == dict.fromkeys(ERROR_KINDS, 0)
    assert classify_connection_error(EngineClosingError("QueuePool closing")) is None


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "(1226, 'User has exceeded the max_user_connections')",
            "max_user_connections",
        ),
        ("(1040, 'Too many connections')", "too_many_connections"),
        ('(2003, "Can\'t connect to MySQL server")', None),
    ],
)
def test_handle_error_counts_connect_errors_by_class(
    message: str, expected: str | None
) -> None:
    """One connect error is counted exactly once, under its own class (+1, not +2)."""
    seen: list[str] = []
    engine = build_engine(SQLITE, "adhoc", on_error=seen.append)
    attempts = _failing_connect(engine, message)
    before = _errors()
    with pytest.raises(sa.exc.OperationalError):
        engine.connect()
    assert len(attempts) == 1
    want = dict.fromkeys(ERROR_KINDS, 0)
    if expected:
        want[expected] = 1
    assert _delta(before) == want
    assert seen == ([expected] if expected else [])


def test_handle_error_classifies_query_errors() -> None:
    engine = build_engine(SQLITE, "adhoc")
    before = _errors()
    with pytest.raises(Exception) as error:
        with engine.connect() as connection:
            connection.execute(sa.text("select * from missing"))
    assert "missing" in str(error.value)
    assert _delta(before) == dict.fromkeys(ERROR_KINDS, 0)


def test_unknown_role_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown MySQL pool role") as error:
        role_options("unknown")
    assert "unknown" in str(error.value)


def test_closing_engine_refuses_new_checkouts() -> None:
    engine = build_engine(SQLITE, "adhoc")
    mark_engine_closing(engine)
    with pytest.raises(EngineClosingError, match="closing") as error:
        engine.connect()
    assert "closing" in str(error.value)


def test_no_dbapi_connect_after_closing() -> None:
    """Once closing, neither engine.connect() nor the raw pool reaches the DBAPI."""
    engine = build_engine(SQLITE, "adhoc")
    attempts: list[int] = []

    @event.listens_for(engine, "do_connect")
    def _count(dialect, conn_rec, cargs, cparams):
        attempts.append(1)

    with engine.connect():
        pass
    assert attempts == [1]
    mark_engine_closing(engine)
    for _ in range(3):
        with pytest.raises(EngineClosingError):
            engine.connect()
        with pytest.raises(EngineClosingError):
            engine.pool.connect()
    assert attempts == [1]


def test_pool_timeout_is_counted_once_by_the_pool_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MYSQL_POOL_SIZE_CRON", "1")
    monkeypatch.setenv("MYSQL_MAX_OVERFLOW_CRON", "0")
    monkeypatch.setenv("MYSQL_POOL_TIMEOUT_CRON", "0")
    seen: list[str] = []
    engine = build_engine(MYSQL, "cron", pool_pre_ping=False, on_error=seen.append)
    engine.dialect.initialize = lambda connection: None  # no server to probe

    @event.listens_for(engine, "do_connect")
    def _connect(dialect, conn_rec, cargs, cparams):
        return MagicMock()  # a stand-in DBAPI connection; nothing reaches MySQL

    held = engine.pool.connect()
    before = _errors()
    with pytest.raises(sa.exc.TimeoutError):
        engine.pool.connect()
    assert _delta(before) == {**dict.fromkeys(ERROR_KINDS, 0), "pool_timeout": 1}
    assert seen == ["pool_timeout"]
    held.close()
    engine.dispose()


def test_pool_events_move_the_gauges() -> None:
    labels = {"role": "adhoc"}

    def gauge(name: str) -> float:
        return _sample(f"data_manager_mysql_{name}", **labels)

    in_use, open_ = gauge("pool_in_use"), gauge("connections_open")
    engine = build_engine(SQLITE, "adhoc")
    connection = engine.connect()
    assert gauge("pool_in_use") == in_use + 1
    assert gauge("connections_open") == open_ + 1
    connection.close()
    assert gauge("pool_in_use") == in_use
    assert gauge("connections_open") == open_ + 1  # pooled, still open
    engine.dispose()
    assert gauge("connections_open") == open_


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        ("(1226, 'max_user_connections')", "max_user_connections"),
        ("(1040, 'too many connections')", "too_many_connections"),
        ("QueuePool limit reached", "pool_timeout"),
    ],
)
def test_connection_error_classification(message: str, kind: str) -> None:
    assert classify_connection_error(RuntimeError(message)) == kind


def test_adhoc_stays_one_plus_one_under_serving_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in {
        "MYSQL_POOL_SIZE": "9",
        "MYSQL_MAX_OVERFLOW": "9",
        "MYSQL_POOL_SIZE_SERVING": "9",
        "MYSQL_MAX_OVERFLOW_SERVING": "9",
    }.items():
        monkeypatch.setenv(name, value)
    assert role_options("adhoc") == {
        "pool_size": 1,
        "max_overflow": 1,
        "pool_timeout": 5,
    }
    engine = build_engine(MYSQL, "adhoc", pool_pre_ping=False)
    assert (engine.pool.size(), engine.pool._max_overflow) == (1, 1)
    assert _sample("data_manager_mysql_pool_cap", role="adhoc") == 2


@pytest.mark.parametrize("role", sorted(ROLE_DEFAULTS))
def test_every_role_builds_its_declared_cap(role: str) -> None:
    size, overflow, timeout = ROLE_DEFAULTS[role]
    engine = build_engine(MYSQL, role, pool_pre_ping=False)
    assert engine.pool.size() == size
    assert engine.pool._max_overflow == overflow
    assert engine.pool._timeout == timeout
    assert _sample("data_manager_mysql_pool_cap", role=role) == size + overflow


def test_stop_rule_caps_are_unchanged() -> None:
    assert ROLE_DEFAULTS["serving"] == (5, 7, 5)
    assert ROLE_DEFAULTS["cron"] == (1, 0, 5)
    assert ROLE_DEFAULTS["job"] == (1, 0, 5)


def test_adapter_rejects_unknown_kwargs() -> None:
    with pytest.raises(
        TypeError, match="Unknown MySQL adapter options: max_overflow, pool_size"
    ):
        MySQLAdapter(MYSQL, role="serving", pool_size=3, max_overflow=1)
    MySQLAdapter(MYSQL, role="serving", pool_recycle=10, pool_pre_ping=False)


POOL_KEYS = {"pool_size", "max_overflow", "poolclass"}
# The only dict literals outside engine_factory.py allowed to name a pool key: the
# health route echoes the configured caps to the operator and builds no pool.
ALLOWED_POOL_KEY_LITERALS = {
    ("health.py", "pool_size"),
    ("health.py", "max_overflow"),
}


def _violations(source: str, filename: str) -> list[str]:
    tree = ast.parse(source, filename=filename)
    name_of_file = Path(filename).name
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", "")
            )
            if name == "create_engine" or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "connect"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "pymysql"
            ):
                violations.append(f"{filename}:{node.lineno}: {name}")
            if any(keyword.arg in POOL_KEYS for keyword in node.keywords):
                violations.append(f"{filename}:{node.lineno}: pool keyword")
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if (
                    isinstance(key, ast.Constant)
                    and key.value in POOL_KEYS
                    and (name_of_file, key.value) not in ALLOWED_POOL_KEY_LITERALS
                ):
                    violations.append(f"{filename}:{node.lineno}: pool key {key.value}")
    return violations


def test_connection_construction_is_centralized() -> None:
    root = Path(__file__).parents[1] / "data_manager"
    violations: list[str] = []
    for path in root.rglob("*.py"):
        if path.name == "engine_factory.py":
            continue
        violations.extend(_violations(path.read_text(encoding="utf-8"), str(path)))
    assert violations == []


def test_guard_allows_only_the_exact_health_literals() -> None:
    assert (
        _violations('x = {"pool_size": 1, "max_overflow": 2}', "routes/health.py") == []
    )
    assert _violations('x = {"poolclass": 1}', "routes/health.py")
    assert _violations('x = {"pool_size": 1}', "routes/other.py")
    assert _violations('x = {"max_overflow": 1}', "api/health_extra.py")
    assert _violations("e = create_engine(u)", "routes/health.py")
    assert _violations("f(pool_size=3)", "routes/health.py")
