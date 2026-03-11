"""
Tests for MongoDB adapter.
"""

from decimal import Decimal
import pytest
from data_manager.db.mongodb_adapter import MongoDBAdapter

def test_prepare_for_bson_basic():
    """Test basic conversion of Decimal to float and int keys to string."""
    data = {
        "price": Decimal("50000.00"),
        1: "one",
        "nested": {
            2: Decimal("2.0"),
            "list": [Decimal("3.0"), {"four": Decimal("4.0")}]
        }
    }
    
    prepared = MongoDBAdapter._prepare_for_bson(data)
    
    assert prepared["price"] == 50000.0
    assert isinstance(prepared["price"], float)
    assert prepared["1"] == "one"
    assert "1" in prepared
    assert 1 not in prepared
    
    assert prepared["nested"]["2"] == 2.0
    assert isinstance(prepared["nested"]["2"], float)
    
    assert prepared["nested"]["list"][0] == 3.0
    assert isinstance(prepared["nested"]["list"][0], float)
    assert prepared["nested"]["list"][1]["four"] == 4.0
    assert isinstance(prepared["nested"]["list"][1]["four"], float)

def test_prepare_for_bson_no_mutation():
    """Test that original dictionary is not mutated."""
    data = {1: Decimal("1.0")}
    original_data = data.copy()
    
    prepared = MongoDBAdapter._prepare_for_bson(data)
    
    assert data == original_data
    assert prepared != data
    assert "1" in prepared
    assert 1 in data

def test_prepare_for_bson_complex_types():
    """Test conversion with other types remaining unchanged."""
    import datetime
    now = datetime.datetime.now()
    data = {
        "time": now,
        "bool": True,
        "none": None,
        "str": "string",
        "int": 123
    }
    
    prepared = MongoDBAdapter._prepare_for_bson(data)
    
    assert prepared["time"] == now
    assert prepared["bool"] is True
    assert prepared["none"] is None
    assert prepared["str"] == "string"
    assert prepared["int"] == 123

def test_prepare_for_bson_key_collision(caplog):
    """Test that key collisions are handled (logged)."""
    import logging
    caplog.set_level(logging.WARNING)
    data = {
        1: "integer one",
        "1": "string one"
    }
    
    prepared = MongoDBAdapter._prepare_for_bson(data)
    
    # One will overwrite the other in the resulting dict
    assert "1" in prepared
    assert len(prepared) == 1
    
    # Check that a warning was logged
    assert any("Key collision detected" in record.message for record in caplog.records)
