"""Tests for the non-fatal operator migration startup check."""

from unittest.mock import MagicMock, patch

import pytest

from data_manager.db.migration_status import (
    OPERATOR_MIGRATIONS,
    unapplied_migrations,
)
from data_manager.main import DataManagerApp


def _engine(rows):
    connection = MagicMock()
    connection.execute.side_effect = [MagicMock(first=lambda: (1,)), rows]
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = connection
    return engine


def test_unapplied_migrations_preserves_repository_order():
    engine = _engine([(OPERATOR_MIGRATIONS[1],)])

    assert unapplied_migrations(engine) == [
        OPERATOR_MIGRATIONS[0],
        OPERATOR_MIGRATIONS[2],
    ]


def test_unapplied_migrations_reports_all_when_tracking_table_is_missing():
    connection = MagicMock()
    connection.execute.return_value.first.return_value = None
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = connection

    assert unapplied_migrations(engine) == list(OPERATOR_MIGRATIONS)


@pytest.mark.asyncio
async def test_startup_check_warns_with_missing_ids(caplog):
    app = DataManagerApp()
    app.db_manager = MagicMock()
    app.db_manager.mysql_adapter.engine = MagicMock()

    with patch(
        "data_manager.main.unapplied_migrations",
        return_value=[OPERATOR_MIGRATIONS[0]],
    ):
        await app._check_schema_migrations()

    assert "Unapplied operator migrations" in caplog.text
    assert OPERATOR_MIGRATIONS[0] in caplog.text


@pytest.mark.asyncio
async def test_startup_check_is_quiet_when_all_migrations_are_applied(caplog):
    app = DataManagerApp()
    app.db_manager = MagicMock()
    app.db_manager.mysql_adapter.engine = MagicMock()

    with patch("data_manager.main.unapplied_migrations", return_value=[]):
        await app._check_schema_migrations()

    assert "Unapplied operator migrations" not in caplog.text


@pytest.mark.asyncio
async def test_startup_check_warns_and_continues_on_query_error(caplog):
    app = DataManagerApp()
    app.db_manager = MagicMock()
    app.db_manager.mysql_adapter.engine = MagicMock()

    with patch(
        "data_manager.main.unapplied_migrations",
        side_effect=RuntimeError("tracking table unavailable"),
    ):
        await app._check_schema_migrations()

    assert "Unable to check operator migration status" in caplog.text
