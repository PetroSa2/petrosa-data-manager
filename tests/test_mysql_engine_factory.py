from __future__ import annotations

import pytest

from data_manager.db.engine_factory import (
    build_engine,
    classify_connection_error,
    mark_engine_closing,
    role_options,
)


def test_role_defaults_and_suffix_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MYSQL_POOL_SIZE_CRON", "3")
    monkeypatch.setenv("MYSQL_MAX_OVERFLOW_CRON", "2")
    monkeypatch.setenv("MYSQL_POOL_TIMEOUT_CRON", "4")
    assert role_options("cron") == {
        "pool_size": 3,
        "max_overflow": 2,
        "pool_timeout": 4,
    }


def test_invalid_role_env_uses_default() -> None:
    assert role_options("adhoc") == {
        "pool_size": 1,
        "max_overflow": 1,
        "pool_timeout": 5,
    }


def test_unknown_role_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown MySQL pool role") as error:
        role_options("unknown")
    assert "unknown" in str(error.value)


def test_closing_engine_refuses_new_checkouts() -> None:
    engine = build_engine("sqlite+pysqlite:///:memory:", "adhoc")
    mark_engine_closing(engine)
    with pytest.raises(RuntimeError, match="closing") as error:
        engine.connect()
    assert "closing" in str(error.value)


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
