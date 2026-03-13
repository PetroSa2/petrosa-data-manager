"""
Unit tests for MySQLAdapter.
"""

from unittest.mock import MagicMock, patch
import pytest
from data_manager.db.mysql_adapter import MySQLAdapter

def test_klines_table_mapping_logic():
    """Test that _get_table handles both financial and binance style suffixes."""
    adapter = MySQLAdapter(connection_string="mysql+pymysql://user:pass@host/db")
    adapter.engine = MagicMock()
    adapter.metadata = MagicMock()
    
    # Mock _create_klines_table to just return a mock Table
    mock_table = MagicMock()
    with patch.object(adapter, '_create_klines_table', return_value=mock_table):
        # Case 1: klines_1h (Binance style)
        table1 = adapter._get_table("klines_1h")
        assert table1 == mock_table
        adapter._create_klines_table.assert_called_with("1h", collection_name="klines_1h")
        
        # Case 2: klines_h1 (Financial style)
        adapter._create_klines_table.reset_mock()
        table2 = adapter._get_table("klines_h1")
        assert table2 == mock_table
        adapter._create_klines_table.assert_called_with("1h", collection_name="klines_h1")

@patch("data_manager.db.mysql_adapter.create_engine")
def test_mysql_adapter_init(mock_create_engine):
    """Test basic initialization."""
    adapter = MySQLAdapter(connection_string="mysql+pymysql://user:pass@host/db")
    assert adapter.connection_string == "mysql+pymysql://user:pass@host/db"
