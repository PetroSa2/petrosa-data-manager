"""Shared MySQL connection-session configuration."""

from typing import Any

from sqlalchemy import event


def set_utc_session(dbapi_connection: Any, _connection_record: Any) -> None:
    """Set one newly-created DB-API connection to UTC."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("SET time_zone = '+00:00'")
    finally:
        cursor.close()


def configure_utc_session(engine: Any) -> None:
    """Register the UTC session hook on an SQLAlchemy engine."""
    event.listen(engine, "connect", set_utc_session)
