"""Periodic read-only MySQL server status metrics."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from prometheus_client import Counter, Gauge

logger = logging.getLogger(__name__)

COLLECTION_INTERVAL_SECONDS = 60

mysql_server_threads_connected = Gauge(
    "data_manager_mysql_server_threads_connected",
    "Current MySQL server threads connected",
)
mysql_server_max_used_connections = Gauge(
    "data_manager_mysql_server_max_used_connections",
    "Maximum MySQL server connections used",
)
mysql_server_aborted_clients = Gauge(
    "data_manager_mysql_server_aborted_clients",
    "MySQL server aborted clients",
)
mysql_server_aborted_connects = Gauge(
    "data_manager_mysql_server_aborted_connects",
    "MySQL server aborted connects",
)
mysql_server_max_user_connections = Gauge(
    "data_manager_mysql_server_max_user_connections",
    "Configured MySQL server user connection limit",
)
mysql_server_wait_timeout = Gauge(
    "data_manager_mysql_server_wait_timeout",
    "Configured MySQL server wait timeout",
)
mysql_user_threads_connected = Gauge(
    "data_manager_mysql_user_threads_connected",
    "Current MySQL connections for the authenticated account",
)
mysql_user_idle_connections = Gauge(
    "data_manager_mysql_user_idle_connections",
    "Idle MySQL connections for the authenticated account",
)
mysql_user_active_connections = Gauge(
    "data_manager_mysql_user_active_connections",
    "Active MySQL connections for the authenticated account",
)
mysql_server_status_errors_total = Counter(
    "data_manager_mysql_server_status_errors_total",
    "MySQL server status collection errors",
)

_GAUGES: dict[str, Gauge] = {
    "Threads_connected": mysql_server_threads_connected,
    "Max_used_connections": mysql_server_max_used_connections,
    "Aborted_clients": mysql_server_aborted_clients,
    "Aborted_connects": mysql_server_aborted_connects,
    "max_user_connections": mysql_server_max_user_connections,
    "wait_timeout": mysql_server_wait_timeout,
    "User_threads_connected": mysql_user_threads_connected,
    "User_idle_connections": mysql_user_idle_connections,
    "User_active_connections": mysql_user_active_connections,
}


def collect_mysql_server_status(adapter: Any) -> None:
    """Read server counters through the adapter and update their gauges."""
    try:
        values = adapter.get_server_status()
        missing = []
        invalid = []
        for name, gauge in _GAUGES.items():
            value = values.get(name)
            if value is None:
                missing.append(name)
                continue
            try:
                gauge.set(float(value))
            except (TypeError, ValueError):
                invalid.append(name)
        if missing or invalid:
            mysql_server_status_errors_total.inc()
            logger.warning(
                "MySQL server status collection skipped invalid values fields=%s",
                [*missing, *invalid],
            )
    except Exception as exc:
        mysql_server_status_errors_total.inc()
        logger.exception(
            "MySQL server status collection failed exception=%s message=%r",
            type(exc).__name__,
            str(exc),
        )


async def mysql_server_status_loop(
    adapter_getter: Callable[[], Any],
    stop_event: asyncio.Event,
) -> None:
    """Refresh MySQL server status until the application shutdown event fires."""
    while not stop_event.is_set():
        adapter = adapter_getter()
        if adapter is not None:
            await asyncio.to_thread(collect_mysql_server_status, adapter)
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=COLLECTION_INTERVAL_SECONDS
            )
        except TimeoutError:
            pass
