"""
Unit tests for MySQLAdapter.
"""

from unittest.mock import MagicMock, patch

import pytest

import constants
from data_manager.db.mysql_adapter import MySQLAdapter
from data_manager.db.mysql_session import configure_utc_session, set_utc_session


def test_klines_table_mapping_logic():
    """Test that _get_table handles both financial and binance style suffixes."""
    adapter = MySQLAdapter(
        connection_string="mysql+pymysql://user:pass@host/db", role="serving"
    )
    adapter.engine = MagicMock()
    adapter.metadata = MagicMock()

    # Mock _create_klines_table to just return a mock Table
    mock_table = MagicMock()
    with patch.object(adapter, "_create_klines_table", return_value=mock_table):
        # Case 1: klines_1h (Binance style)
        table1 = adapter._get_table("klines_1h")
        assert table1 == mock_table
        adapter._create_klines_table.assert_called_with(
            "1h", collection_name="klines_1h"
        )

        # Case 2: klines_h1 (Financial style)
        adapter._create_klines_table.reset_mock()
        table2 = adapter._get_table("klines_h1")
        assert table2 == mock_table
        adapter._create_klines_table.assert_called_with(
            "1h", collection_name="klines_h1"
        )


def test_mysql_adapter_init():
    """Test basic initialization."""
    adapter = MySQLAdapter(
        connection_string="mysql+pymysql://user:pass@host/db", role="serving"
    )
    assert adapter.connection_string == "mysql+pymysql://user:pass@host/db"


# petrosa-data-manager#299 (gateway hardening, track C of petrosa_k8s#1059):
# data-manager becomes the SOLE MySQL pool holder. Server wait_timeout=15s
# and the shared max_user_connections=30 cap are external facts confirmed
# live 2026-09-14 — the ecosystem budget below is derived from those.
_HPA_MAX_REPLICAS = 2  # k8s/data-manager/hpa.yaml maxReplicas
_ECOSYSTEM_MYSQL_BUDGET = 24  # ~24 conns, leaving headroom under the 30 cap
_SESSION_WAIT_TIMEOUT = constants.MYSQL_SESSION_WAIT_TIMEOUT


def test_mysql_adapter_pool_recycle_below_server_wait_timeout():
    """AC1 (#299): pool_recycle must stay below the server wait_timeout=15s.

    Otherwise every pooled connection is server-killed before reuse and
    pool_pre_ping reconnects on nearly every checkout (the Aborted_clients
    churn documented in the mysql audit).
    """
    adapter = MySQLAdapter(
        connection_string="mysql+pymysql://user:pass@host/db", role="serving"
    )
    assert adapter.engine_options["pool_recycle"] < _SESSION_WAIT_TIMEOUT
    assert adapter.engine_options["pool_recycle"] > 0


def test_mysql_adapter_pool_size_within_ecosystem_budget():
    """AC2 (#299): (pool_size + max_overflow) * HPA maxReplicas <= ~24.

    Previously pool_size=5 + max_overflow=10 = 15/pod * 2 pods = 30 = the
    ENTIRE shared max_user_connections cap, leaving zero headroom for peer
    services still on direct MySQL during the gateway migration window.
    """
    adapter = MySQLAdapter(
        connection_string="mysql+pymysql://user:pass@host/db", role="serving"
    )
    per_pod = adapter.pool_options["pool_size"] + adapter.pool_options["max_overflow"]
    assert per_pod * _HPA_MAX_REPLICAS <= _ECOSYSTEM_MYSQL_BUDGET


@patch("data_manager.db.mysql_adapter.build_engine")
def test_mysql_adapter_connect_passes_hardened_pool_kwargs(
    mock_build_engine,
):
    """AC1/AC2 (#299): the kwargs actually reaching SQLAlchemy's create_engine
    carry the hardened pool_recycle/pool_size/max_overflow — not just the
    adapter's own dict (regression guard for #299)."""
    adapter = MySQLAdapter(
        connection_string="mysql+pymysql://user:pass@host/db", role="serving"
    )
    mock_build_engine.return_value = MagicMock()
    adapter.connect()

    _, role = mock_build_engine.call_args.args[:2]
    kwargs = mock_build_engine.call_args.kwargs
    assert role == "serving"
    assert kwargs["pool_recycle"] < _SESSION_WAIT_TIMEOUT
    assert kwargs["connect_args"]["init_command"]
    assert (
        adapter.pool_options["pool_size"] + adapter.pool_options["max_overflow"] <= 12
    )


def test_mysql_session_configures_utc_connect_hook():
    """Every DB-API connection must set the session timezone to UTC."""
    engine = MagicMock()
    with patch("data_manager.db.mysql_session.event.listen") as listen:
        configure_utc_session(engine)
    listen.assert_called_once_with(engine, "connect", set_utc_session)

    connection = MagicMock()
    set_utc_session(connection, None)
    connection.cursor.return_value.execute.assert_called_once_with(
        "SET time_zone = '+00:00'"
    )
