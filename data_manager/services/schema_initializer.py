"""
Schema initializer for bootstrapping common schemas.
"""

import logging
from typing import Dict, List

from data_manager.models.schemas import SchemaRegistration, CompatibilityMode

logger = logging.getLogger(__name__)


class SchemaInitializer:
    """
    Initializer for common schemas used across Petrosa services.
    
    Provides predefined schemas for candles, trades, orders, depth,
    funding rates, health metrics, and audit logs.
    """

    @staticmethod
    def get_common_schemas() -> Dict[str, List[SchemaRegistration]]:
        """
        Get all common schemas organized by database.
        
        Returns:
            Dictionary mapping database names to lists of schema registrations
        """
        return {
            "mongodb": SchemaInitializer._get_mongodb_schemas(),
            "mysql": SchemaInitializer._get_mysql_schemas(),
        }

    @staticmethod
    def _get_mongodb_schemas() -> List[SchemaRegistration]:
        """Get MongoDB schemas for time-series data."""
        return [
            # OHLCV Candle Schema
            SchemaRegistration(
                version=1,
                schema={
                    "type": "object",
                    "required": ["symbol", "timestamp", "open", "high", "low", "close", "volume"],
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "pattern": "^[A-Z]+$",
                            "description": "Trading pair symbol"
                        },
                        "timestamp": {
                            "type": "string",
                            "format": "date-time",
                            "description": "Candle timestamp"
                        },
                        "open": {
                            "type": "number",
                            "minimum": 0,
                            "description": "Opening price"
                        },
                        "high": {
                            "type": "number",
                            "minimum": 0,
                            "description": "Highest price"
                        },
                        "low": {
                            "type": "number",
                            "minimum": 0,
                            "description": "Lowest price"
                        },
                        "close": {
                            "type": "number",
                            "minimum": 0,
                            "description": "Closing price"
                        },
                        "volume": {
                            "type": "number",
                            "minimum": 0,
                            "description": "Trading volume"
                        },
                        "quote_volume": {
                            "type": "number",
                            "minimum": 0,
                            "description": "Quote asset volume"
                        },
                        "trades_count": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "Number of trades"
                        },
                        "taker_buy_volume": {
                            "type": "number",
                            "minimum": 0,
                            "description": "Taker buy volume"
                        },
                        "taker_buy_quote_volume": {
                            "type": "number",
                            "minimum": 0,
                            "description": "Taker buy quote volume"
                        }
                    },
                    "additionalProperties": False
                },
                compatibility_mode=CompatibilityMode.BACKWARD,
                description="OHLCV candle data schema for time-series storage",
                created_by="system"
            ),
            
            # Trade Schema
            SchemaRegistration(
                version=1,
                schema={
                    "type": "object",
                    "required": ["symbol", "timestamp", "price", "quantity", "side"],
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "pattern": "^[A-Z]+$",
                            "description": "Trading pair symbol"
                        },
                        "timestamp": {
                            "type": "string",
                            "format": "date-time",
                            "description": "Trade timestamp"
                        },
                        "price": {
                            "type": "number",
                            "minimum": 0,
                            "description": "Trade price"
                        },
                        "quantity": {
                            "type": "number",
                            "minimum": 0,
                            "description": "Trade quantity"
                        },
                        "side": {
                            "type": "string",
                            "enum": ["BUY", "SELL"],
                            "description": "Trade side"
                        },
                        "trade_id": {
                            "type": "string",
                            "description": "Unique trade identifier"
                        },
                        "buyer_order_id": {
                            "type": "string",
                            "description": "Buyer order ID"
                        },
                        "seller_order_id": {
                            "type": "string",
                            "description": "Seller order ID"
                        },
                        "is_buyer_maker": {
                            "type": "boolean",
                            "description": "Whether buyer is maker"
                        }
                    },
                    "additionalProperties": False
                },
                compatibility_mode=CompatibilityMode.BACKWARD,
                description="Trade execution data schema",
                created_by="system"
            ),
            
            # Order Book Depth Schema
            SchemaRegistration(
                version=1,
                schema={
                    "type": "object",
                    "required": ["symbol", "timestamp", "bids", "asks"],
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "pattern": "^[A-Z]+$",
                            "description": "Trading pair symbol"
                        },
                        "timestamp": {
                            "type": "string",
                            "format": "date-time",
                            "description": "Depth timestamp"
                        },
                        "bids": {
                            "type": "array",
                            "items": {
                                "type": "array",
                                "items": {"type": "number"},
                                "minItems": 2,
                                "maxItems": 2
                            },
                            "description": "Bid orders [price, quantity]"
                        },
                        "asks": {
                            "type": "array",
                            "items": {
                                "type": "array",
                                "items": {"type": "number"},
                                "minItems": 2,
                                "maxItems": 2
                            },
                            "description": "Ask orders [price, quantity]"
                        },
                        "last_update_id": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "Last update ID"
                        }
                    },
                    "additionalProperties": False
                },
                compatibility_mode=CompatibilityMode.BACKWARD,
                description="Order book depth data schema",
                created_by="system"
            ),
            
            # Funding Rate Schema
            SchemaRegistration(
                version=1,
                schema={
                    "type": "object",
                    "required": ["symbol", "timestamp", "funding_rate"],
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "pattern": "^[A-Z]+$",
                            "description": "Trading pair symbol"
                        },
                        "timestamp": {
                            "type": "string",
                            "format": "date-time",
                            "description": "Funding rate timestamp"
                        },
                        "funding_rate": {
                            "type": "number",
                            "description": "Funding rate value"
                        },
                        "funding_time": {
                            "type": "string",
                            "format": "date-time",
                            "description": "Next funding time"
                        },
                        "mark_price": {
                            "type": "number",
                            "minimum": 0,
                            "description": "Mark price"
                        },
                        "index_price": {
                            "type": "number",
                            "minimum": 0,
                            "description": "Index price"
                        }
                    },
                    "additionalProperties": False
                },
                compatibility_mode=CompatibilityMode.BACKWARD,
                description="Funding rate data schema",
                created_by="system"
            )
        ]

    @staticmethod
    def _get_mysql_schemas() -> List[SchemaRegistration]:
        """Get MySQL schemas for structured data."""
        return [
            # Order Schema
            SchemaRegistration(
                version=1,
                schema={
                    "type": "object",
                    "required": ["order_id", "symbol", "side", "type", "status"],
                    "properties": {
                        "order_id": {
                            "type": "string",
                            "description": "Unique order identifier"
                        },
                        "symbol": {
                            "type": "string",
                            "pattern": "^[A-Z]+$",
                            "description": "Trading pair symbol"
                        },
                        "side": {
                            "type": "string",
                            "enum": ["BUY", "SELL"],
                            "description": "Order side"
                        },
                        "type": {
                            "type": "string",
                            "enum": ["LIMIT", "MARKET", "STOP_LOSS", "STOP_LOSS_LIMIT", "TAKE_PROFIT", "TAKE_PROFIT_LIMIT"],
                            "description": "Order type"
                        },
                        "status": {
                            "type": "string",
                            "enum": ["NEW", "PARTIALLY_FILLED", "FILLED", "CANCELED", "PENDING_CANCEL", "REJECTED", "EXPIRED"],
                            "description": "Order status"
                        },
                        "quantity": {
                            "type": "number",
                            "minimum": 0,
                            "description": "Order quantity"
                        },
                        "price": {
                            "type": "number",
                            "minimum": 0,
                            "description": "Order price"
                        },
                        "stop_price": {
                            "type": "number",
                            "minimum": 0,
                            "description": "Stop price for stop orders"
                        },
                        "time_in_force": {
                            "type": "string",
                            "enum": ["GTC", "IOC", "FOK"],
                            "description": "Time in force"
                        },
                        "created_at": {
                            "type": "string",
                            "format": "date-time",
                            "description": "Order creation timestamp"
                        },
                        "updated_at": {
                            "type": "string",
                            "format": "date-time",
                            "description": "Last update timestamp"
                        },
                        "client_order_id": {
                            "type": "string",
                            "description": "Client order identifier"
                        },
                        "strategy_id": {
                            "type": "string",
                            "description": "Associated strategy ID"
                        }
                    },
                    "additionalProperties": False
                },
                compatibility_mode=CompatibilityMode.BACKWARD,
                description="Order management schema for structured storage",
                created_by="system"
            ),
            
            # Health Metrics Schema
            SchemaRegistration(
                version=1,
                schema={
                    "type": "object",
                    "required": ["dataset_id", "symbol", "timestamp"],
                    "properties": {
                        "dataset_id": {
                            "type": "string",
                            "description": "Dataset identifier"
                        },
                        "symbol": {
                            "type": "string",
                            "pattern": "^[A-Z]+$",
                            "description": "Trading pair symbol"
                        },
                        "timestamp": {
                            "type": "string",
                            "format": "date-time",
                            "description": "Metrics timestamp"
                        },
                        "completeness": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 100,
                            "description": "Data completeness percentage"
                        },
                        "freshness_seconds": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "Data freshness in seconds"
                        },
                        "gaps_count": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "Number of data gaps"
                        },
                        "duplicates_count": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "Number of duplicate records"
                        },
                        "quality_score": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 100,
                            "description": "Overall quality score"
                        },
                        "anomalies_detected": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "Number of anomalies detected"
                        },
                        "last_data_point": {
                            "type": "string",
                            "format": "date-time",
                            "description": "Last data point timestamp"
                        }
                    },
                    "additionalProperties": False
                },
                compatibility_mode=CompatibilityMode.BACKWARD,
                description="Health metrics schema for data quality monitoring",
                created_by="system"
            ),
            
            # Audit Log Schema
            SchemaRegistration(
                version=1,
                schema={
                    "type": "object",
                    "required": ["audit_id", "dataset_id", "symbol", "audit_type", "timestamp"],
                    "properties": {
                        "audit_id": {
                            "type": "string",
                            "description": "Unique audit identifier"
                        },
                        "dataset_id": {
                            "type": "string",
                            "description": "Dataset identifier"
                        },
                        "symbol": {
                            "type": "string",
                            "pattern": "^[A-Z]+$",
                            "description": "Trading pair symbol"
                        },
                        "audit_type": {
                            "type": "string",
                            "enum": ["DATA_QUALITY", "ANOMALY", "GAP", "DUPLICATE", "MISSING", "INVALID"],
                            "description": "Type of audit"
                        },
                        "severity": {
                            "type": "string",
                            "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"],
                            "description": "Audit severity level"
                        },
                        "timestamp": {
                            "type": "string",
                            "format": "date-time",
                            "description": "Audit timestamp"
                        },
                        "details": {
                            "type": "string",
                            "description": "Audit details and description"
                        },
                        "affected_records": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "Number of affected records"
                        },
                        "resolution_status": {
                            "type": "string",
                            "enum": ["OPEN", "IN_PROGRESS", "RESOLVED", "IGNORED"],
                            "description": "Resolution status"
                        },
                        "resolved_at": {
                            "type": "string",
                            "format": "date-time",
                            "description": "Resolution timestamp"
                        },
                        "resolved_by": {
                            "type": "string",
                            "description": "User who resolved the audit"
                        }
                    },
                    "additionalProperties": False
                },
                compatibility_mode=CompatibilityMode.BACKWARD,
                description="Audit log schema for data integrity tracking",
                created_by="system"
            ),
            
            # Strategy Signal Schema
            SchemaRegistration(
                version=1,
                schema={
                    "type": "object",
                    "required": ["signal_id", "strategy_id", "symbol", "signal_type", "timestamp"],
                    "properties": {
                        "signal_id": {
                            "type": "string",
                            "description": "Unique signal identifier"
                        },
                        "strategy_id": {
                            "type": "string",
                            "description": "Strategy identifier"
                        },
                        "symbol": {
                            "type": "string",
                            "pattern": "^[A-Z]+$",
                            "description": "Trading pair symbol"
                        },
                        "signal_type": {
                            "type": "string",
                            "enum": ["BUY", "SELL", "HOLD", "CLOSE"],
                            "description": "Signal type"
                        },
                        "timestamp": {
                            "type": "string",
                            "format": "date-time",
                            "description": "Signal timestamp"
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                            "description": "Signal confidence score"
                        },
                        "strength": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                            "description": "Signal strength"
                        },
                        "price_target": {
                            "type": "number",
                            "minimum": 0,
                            "description": "Target price"
                        },
                        "stop_loss": {
                            "type": "number",
                            "minimum": 0,
                            "description": "Stop loss price"
                        },
                        "take_profit": {
                            "type": "number",
                            "minimum": 0,
                            "description": "Take profit price"
                        },
                        "metadata": {
                            "type": "object",
                            "description": "Additional signal metadata"
                        },
                        "created_by": {
                            "type": "string",
                            "description": "Signal creator"
                        }
                    },
                    "additionalProperties": False
                },
                compatibility_mode=CompatibilityMode.BACKWARD,
                description="Strategy signal schema for trading decisions",
                created_by="system"
            )
        ]

    @staticmethod
    def get_schema_by_name(schema_name: str, database: str) -> SchemaRegistration:
        """
        Get a specific schema by name and database.
        
        Args:
            schema_name: Name of the schema
            database: Database type ('mysql' or 'mongodb')
            
        Returns:
            Schema registration
            
        Raises:
            ValueError: If schema not found
        """
        schemas = SchemaInitializer.get_common_schemas()
        
        if database not in schemas:
            raise ValueError(f"Database {database} not supported")
        
        for schema in schemas[database]:
            # Extract schema name from the schema definition
            schema_def_name = schema.schema.get("title", "").lower().replace(" ", "_")
            if schema_def_name == schema_name.lower():
                return schema
        
        raise ValueError(f"Schema {schema_name} not found in {database}")

    @staticmethod
    def list_available_schemas() -> Dict[str, List[str]]:
        """
        List all available schema names by database.
        
        Returns:
            Dictionary mapping database names to lists of schema names
        """
        schemas = SchemaInitializer.get_common_schemas()
        result = {}
        
        for database, schema_list in schemas.items():
            result[database] = []
            for schema in schema_list:
                # Extract schema name from the schema definition
                schema_def_name = schema.schema.get("title", "").lower().replace(" ", "_")
                if not schema_def_name:
                    # Fallback to a generic name based on database
                    schema_def_name = f"{database}_schema_{len(result[database]) + 1}"
                result[database].append(schema_def_name)
        
        return result
