"""Role-aware SQLAlchemy engine construction and pool instrumentation."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from typing import Any

import sqlalchemy as sa
from prometheus_client import Counter, Gauge, Histogram
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from data_manager.db.mysql_session import configure_utc_session

logger = logging.getLogger(__name__)

ROLE_DEFAULTS: dict[str, tuple[int, int, int]] = {
    "serving": (5, 7, 5),
    "cron": (1, 0, 5),
    "job": (1, 0, 5),
    "adhoc": (1, 1, 5),
}
ERROR_KINDS = ("max_user_connections", "too_many_connections", "pool_timeout")

pool_in_use = Gauge(
    "data_manager_mysql_pool_in_use", "MySQL connections checked out", ["role"]
)
connections_open = Gauge(
    "data_manager_mysql_connections_open", "Open MySQL connections", ["role"]
)
pool_cap = Gauge("data_manager_mysql_pool_cap", "Configured MySQL pool cap", ["role"])
pool_in_use_at_checkout = Histogram(
    "data_manager_mysql_pool_in_use_at_checkout",
    "Pool usage observed at checkout",
    ["role"],
    buckets=tuple(range(16)),
)
checkout_wait_seconds = Histogram(
    "data_manager_mysql_checkout_wait_seconds", "Time spent acquiring a connection", ["role"]
)
executor_queue_seconds = Histogram(
    "data_manager_mysql_executor_queue_seconds", "Synchronous executor queue time", ["role"]
)
connection_errors_total = Counter(
    "data_manager_mysql_connection_errors_total",
    "MySQL connection errors by kind",
    ["kind"],
)
for _kind in ERROR_KINDS:
    connection_errors_total.labels(kind=_kind)


def _setting(role: str, name: str, default: int) -> int:
    raw = os.getenv(f"MYSQL_{name}_{role.upper()}")
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def role_options(role: str) -> dict[str, int]:
    """Return validated pool settings for a required workload role."""
    if role not in ROLE_DEFAULTS:
        raise ValueError(f"Unknown MySQL pool role: {role}")
    size, overflow, timeout = ROLE_DEFAULTS[role]
    return {
        "pool_size": _setting(role, "POOL_SIZE", size),
        "max_overflow": _setting(role, "MAX_OVERFLOW", overflow),
        "pool_timeout": _setting(role, "POOL_TIMEOUT", timeout),
    }


def classify_connection_error(exc: BaseException) -> str | None:
    """Map provider and SQLAlchemy pool errors to the bounded metric labels."""
    text = str(exc).lower()
    if "1226" in text or "max_user_connections" in text:
        return "max_user_connections"
    if "1040" in text or "too many connections" in text:
        return "too_many_connections"
    if isinstance(exc, SQLAlchemyTimeoutError) or "queuepool" in text or "pool timeout" in text:
        return "pool_timeout"
    return None


def record_connection_error(exc: BaseException) -> None:
    """Record a classified connection failure when it maps to a budget guard."""
    kind = classify_connection_error(exc)
    if kind:
        connection_errors_total.labels(kind=kind).inc()


def mark_engine_closing(engine: Engine) -> None:
    """Prevent a disposed engine from lazily opening a replacement pool."""
    state = getattr(engine, "_petrosa_pool_state", None)
    if state is not None:
        state["closing"] = True
    engine.dispose()


def build_engine(
    connection_string: str,
    role: str,
    *,
    connect_args: dict[str, Any] | None = None,
    pool_pre_ping: bool = True,
    pool_recycle: int = 90,
    on_error: Callable[[str], None] | None = None,
) -> Engine:
    """Build one instrumented engine for a declared workload role."""
    options = role_options(role)
    args = dict(connect_args or {})
    if connection_string.startswith("mysql"):
        args.setdefault("charset", "utf8mb4")
    engine_kwargs: dict[str, Any] = {
        "pool_pre_ping": pool_pre_ping,
        "pool_recycle": pool_recycle,
        "connect_args": args,
    }
    if connection_string.startswith("mysql"):
        engine_kwargs.update(options)
    engine = sa.create_engine(connection_string, **engine_kwargs)
    state = {"closing": False, "checkout_started": 0.0}
    try:
        engine._petrosa_pool_state = state
    except AttributeError:
        return engine
    if connection_string.startswith("mysql"):
        configure_utc_session(engine)
    pool_cap.labels(role=role).set(options["pool_size"] + options["max_overflow"])

    @event.listens_for(engine, "connect")
    def _connect(dbapi_connection: Any, connection_record: Any) -> None:
        del dbapi_connection, connection_record
        connections_open.labels(role=role).inc()

    @event.listens_for(engine, "close")
    def _close(dbapi_connection: Any, connection_record: Any) -> None:
        del dbapi_connection, connection_record
        connections_open.labels(role=role).dec()

    @event.listens_for(engine, "checkout")
    def _checkout(dbapi_connection: Any, connection_record: Any, proxy: Any) -> None:
        del dbapi_connection, connection_record, proxy
        if state["closing"]:
            error = RuntimeError("MySQL engine is closing")
            connection_errors_total.labels(kind="pool_timeout").inc()
            if on_error:
                on_error("pool_timeout")
            raise error
        state["checkout_started"] = time.monotonic()
        current = pool_in_use.labels(role=role)._value.get()
        pool_in_use_at_checkout.labels(role=role).observe(current)
        pool_in_use.labels(role=role).inc()

    @event.listens_for(engine, "checkin")
    def _checkin(dbapi_connection: Any, connection_record: Any) -> None:
        del dbapi_connection, connection_record
        pool_in_use.labels(role=role).dec()
        started = state.get("checkout_started", 0.0)
        if started:
            checkout_wait_seconds.labels(role=role).observe(max(0.0, time.monotonic() - started))

    return engine


def create_read_only_engine(connection_string: str, role: str) -> Engine:
    """Build a read-only engine through the shared role factory."""
    from constants import (
        MYSQL_POOL_RECYCLE,
        MYSQL_SESSION_SQL_MODE,
        MYSQL_SESSION_WAIT_TIMEOUT,
    )

    return build_engine(
        connection_string,
        role,
        pool_recycle=MYSQL_POOL_RECYCLE,
        connect_args={
            "autocommit": True,
            "init_command": (
                f"SET SESSION sql_mode='{MYSQL_SESSION_SQL_MODE}', "
                f"wait_timeout={MYSQL_SESSION_WAIT_TIMEOUT}"
            ),
        },
    )
