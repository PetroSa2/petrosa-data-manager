"""
MySQL adapter implementation for Data Manager.

Based on petrosa-binance-data-extractor patterns.
"""

import logging
from datetime import datetime
from typing import Any

from pydantic import BaseModel

try:
    import sqlalchemy as sa
    from sqlalchemy import (
        Column,
        DateTime,
        Index,
        Integer,
        MetaData,
        Numeric,
        String,
        Table,
        Text,
        create_engine,
    )
    from sqlalchemy.engine import Engine
    from sqlalchemy.exc import IntegrityError, SQLAlchemyError
    from sqlalchemy.sql import and_, delete, func, select

    SQLALCHEMY_AVAILABLE = True
except ImportError:
    SQLALCHEMY_AVAILABLE = False

import constants
from data_manager.db.base_adapter import BaseAdapter, DatabaseError
from data_manager.utils.circuit_breaker import DatabaseCircuitBreaker

logger = logging.getLogger(__name__)


class MySQLAdapter(BaseAdapter):
    """
    MySQL/MariaDB implementation of the BaseAdapter interface.

    Uses SQLAlchemy for database operations with circuit breaker for reliability.
    """

    def __init__(self, connection_string: str | None = None, **kwargs):
        """
        Initialize MySQL adapter.

        Args:
            connection_string: MySQL connection string
            **kwargs: Additional SQLAlchemy engine options
        """
        if not SQLALCHEMY_AVAILABLE:
            raise ImportError(
                "SQLAlchemy and MySQL driver required. "
                "Install: pip install sqlalchemy pymysql"
            )

        # Use provided connection string or build from constants
        if connection_string is None:
            connection_string = self._build_connection_string()

        super().__init__(connection_string, **kwargs)

        # SQLAlchemy specific settings
        self.engine: Engine | None = None
        self.metadata = MetaData()
        self.tables: dict[str, Table] = {}

        # Circuit breaker for reliability
        self.circuit_breaker = DatabaseCircuitBreaker("mysql")

        # Engine options
        self.engine_options = {
            "pool_pre_ping": True,
            "pool_recycle": 1800,  # Recycle connections after 30 minutes
            "pool_size": 5,  # Conservative for shared resources
            "max_overflow": 10,  # Limited overflow
            "pool_timeout": 30,  # Timeout for connection acquisition
            "connect_args": {
                "charset": "utf8mb4",
                "autocommit": False,  # Explicit transaction control
            },
            **kwargs,
        }

    def _build_connection_string(self) -> str:
        """Build MySQL connection string from constants."""
        user = constants.MYSQL_USER if hasattr(constants, "MYSQL_USER") else "root"
        password = (
            constants.MYSQL_PASSWORD if hasattr(constants, "MYSQL_PASSWORD") else ""
        )
        host = constants.MYSQL_HOST if hasattr(constants, "MYSQL_HOST") else "localhost"
        port = constants.MYSQL_PORT if hasattr(constants, "MYSQL_PORT") else 3306
        database = (
            constants.MYSQL_DB
            if hasattr(constants, "MYSQL_DB")
            else "petrosa_data_manager"
        )

        return f"mysql+pymysql://{user}:{password}@{host}:{port}/{database}"

    def connect(self) -> None:
        """Establish connection to MySQL."""
        try:
            self.engine = create_engine(self.connection_string, **self.engine_options)
            # Test connection
            if self.engine is not None:
                with self.engine.connect() as conn:
                    conn.execute(sa.text("SELECT 1"))
            self._connected = True
            logger.info("Connected to MySQL database")

            # Create tables if they don't exist
            self._create_tables()

        except SQLAlchemyError as e:
            raise DatabaseError(f"Failed to connect to MySQL: {e}") from e

    def disconnect(self) -> None:
        """Close MySQL connection."""
        if self.engine:
            self.engine.dispose()
            self._connected = False
            logger.info("Disconnected from MySQL")

    def _create_tables(self) -> None:
        """Create database tables for metadata, audit, and catalog."""
        # Datasets table
        self.tables["datasets"] = Table(
            "datasets",
            self.metadata,
            Column("dataset_id", String(64), primary_key=True),
            Column("name", String(100), nullable=False),
            Column("description", String(500)),
            Column("category", String(50), nullable=False),
            Column("schema_id", String(64)),
            Column("storage_type", String(20), nullable=False),
            Column("owner", String(100)),
            Column("update_frequency", String(50)),
            Column("created_at", DateTime, nullable=False),
            Column("updated_at", DateTime, nullable=False),
            Index("idx_datasets_category", "category"),
        )

        # Audit logs table
        self.tables["audit_logs"] = Table(
            "audit_logs",
            self.metadata,
            Column("audit_id", String(64), primary_key=True),
            Column("dataset_id", String(64), nullable=False),
            Column("symbol", String(20), nullable=False),
            Column("audit_type", String(50), nullable=False),
            Column("severity", String(20)),
            Column("details", Text),
            Column("timestamp", DateTime, nullable=False),
            Index("idx_audit_logs_dataset_timestamp", "dataset_id", "timestamp"),
            Index("idx_audit_logs_symbol", "symbol"),
        )

        # Health metrics table
        self.tables["health_metrics"] = Table(
            "health_metrics",
            self.metadata,
            Column("metric_id", String(64), primary_key=True),
            Column("dataset_id", String(64), nullable=False),
            Column("symbol", String(20), nullable=False),
            Column("completeness", Numeric(5, 2)),
            Column("freshness_seconds", Integer),
            Column("gaps_count", Integer, default=0),
            Column("duplicates_count", Integer, default=0),
            Column("quality_score", Numeric(5, 2)),
            Column("timestamp", DateTime, nullable=False),
            Index("idx_health_metrics_dataset_timestamp", "dataset_id", "timestamp"),
            Index("idx_health_metrics_symbol", "symbol"),
        )

        # Backfill jobs table
        self.tables["backfill_jobs"] = Table(
            "backfill_jobs",
            self.metadata,
            Column("job_id", String(64), primary_key=True),
            Column("symbol", String(20), nullable=False),
            Column("data_type", String(50), nullable=False),
            Column("timeframe", String(10)),
            Column("start_time", DateTime, nullable=False),
            Column("end_time", DateTime, nullable=False),
            Column("status", String(20), nullable=False),
            Column("progress", Numeric(5, 2), default=0),
            Column("records_fetched", Integer, default=0),
            Column("records_inserted", Integer, default=0),
            Column("error_message", Text),
            Column("created_at", DateTime, nullable=False),
            Column("started_at", DateTime),
            Column("completed_at", DateTime),
            Index("idx_backfill_jobs_status", "status"),
            Index("idx_backfill_jobs_symbol", "symbol"),
        )

        # Lineage records table
        self.tables["lineage_records"] = Table(
            "lineage_records",
            self.metadata,
            Column("lineage_id", String(64), primary_key=True),
            Column("dataset_id", String(64), nullable=False),
            Column("source_dataset_id", String(64)),
            Column("transformation", String(100), nullable=False),
            Column("input_records", Integer),
            Column("output_records", Integer),
            Column("timestamp", DateTime, nullable=False),
            Index("idx_lineage_records_dataset", "dataset_id"),
        )

        # Schemas table for schema registry
        self.tables["schemas"] = Table(
            "schemas",
            self.metadata,
            Column("schema_id", String(64), primary_key=True),
            Column("name", String(100), nullable=False),
            Column("version", Integer, nullable=False),
            Column("schema_json", Text, nullable=False),
            Column("compatibility_mode", String(20)),
            Column("status", String(20), nullable=False),
            Column("description", Text),
            Column("created_at", DateTime, nullable=False),
            Column("updated_at", DateTime, nullable=False),
            Column("created_by", String(100)),
            Index("idx_schemas_name", "name"),
            Index("idx_schemas_status", "status"),
            Index("idx_schemas_name_version", "name", "version", unique=True),
        )

        # Create all tables
        if self.engine is not None:
            self.metadata.create_all(self.engine)
            logger.info("MySQL tables created/verified")

    def _get_table(self, collection: str) -> "Table":
        """Get table object for collection."""
        if collection in self.tables:
            return self.tables[collection]
        else:
            raise DatabaseError(f"Unknown collection: {collection}")

    def write(self, model_instances: list[BaseModel], collection: str) -> int:
        """Write model instances to MySQL table with circuit breaker protection."""
        if not self._connected:
            raise DatabaseError("Not connected to database")

        if not model_instances:
            return 0

        def _write_operation():
            try:
                table = self._get_table(collection)

                # Convert models to dictionaries
                records = []
                for instance in model_instances:
                    record = instance.model_dump()
                    # Ensure datetime fields are proper datetime objects
                    for key, value in record.items():
                        if isinstance(value, str) and key.endswith(
                            ("_at", "timestamp")
                        ):
                            try:
                                record[key] = datetime.fromisoformat(
                                    value.replace("Z", "+00:00")
                                )
                            except (ValueError, TypeError):
                                pass
                    records.append(record)

                # Insert records
                engine = self._ensure_connected()
                with engine.connect() as conn:
                    trans = conn.begin()
                    try:
                        # Use INSERT IGNORE to handle duplicates
                        stmt = table.insert().prefix_with("IGNORE")
                        result = conn.execute(stmt, records)
                        trans.commit()
                        return result.rowcount
                    except Exception:
                        trans.rollback()
                        raise

            except IntegrityError:
                logger.warning("Duplicate records found when writing to %s", collection)
                return 0
            except Exception as e:
                logger.warning(f"MySQL write error: {e}")
                raise DatabaseError(f"Error in MySQL write: {e}") from e

        # Use circuit breaker for write operation
        return self.circuit_breaker.call(_write_operation)

    def write_batch(
        self, model_instances: list[BaseModel], collection: str, batch_size: int = 1000
    ) -> int:
        """Write model instances in batches."""
        total_written = 0

        for i in range(0, len(model_instances), batch_size):
            batch = model_instances[i : i + batch_size]
            written = self.write(batch, collection)
            total_written += written

            if i + batch_size < len(model_instances):
                logger.debug(
                    "Written batch %d: %d records to %s",
                    i // batch_size + 1,
                    written,
                    collection,
                )

        return total_written

    def query_range(
        self,
        collection: str,
        start: datetime,
        end: datetime,
        symbol: str | None = None,
    ) -> list[dict[str, Any]]:
        """Query records within time range."""
        if not self._connected:
            raise DatabaseError("Not connected to database")

        try:
            table = self._get_table(collection)

            # Build query
            query = select(table).where(
                and_(table.c.timestamp >= start, table.c.timestamp < end)
            )

            if symbol:
                query = query.where(table.c.symbol == symbol)

            query = query.order_by(table.c.timestamp)

            engine = self._ensure_connected()
            with engine.connect() as conn:
                result = conn.execute(query)
                return [dict(row._mapping) for row in result]

        except Exception as e:
            raise DatabaseError(f"Failed to query range from {collection}: {e}") from e

    def query_latest(
        self, collection: str, symbol: str | None = None, limit: int = 1
    ) -> list[dict[str, Any]]:
        """Query most recent records."""
        if not self._connected:
            raise DatabaseError("Not connected to database")

        try:
            table = self._get_table(collection)

            query = select(table)
            if symbol:
                query = query.where(table.c.symbol == symbol)

            query = query.order_by(table.c.timestamp.desc()).limit(limit)

            engine = self._ensure_connected()
            with engine.connect() as conn:
                result = conn.execute(query)
                return [dict(row._mapping) for row in result]

        except Exception as e:
            raise DatabaseError(f"Failed to query latest from {collection}: {e}") from e

    def get_record_count(
        self,
        collection: str,
        start: datetime | None = None,
        end: datetime | None = None,
        symbol: str | None = None,
    ) -> int:
        """Get count of records matching criteria."""
        if not self._connected:
            raise DatabaseError("Not connected to database")

        try:
            table = self._get_table(collection)

            query = select(func.count()).select_from(table)

            conditions = []
            if start:
                conditions.append(table.c.timestamp >= start)
            if end:
                conditions.append(table.c.timestamp < end)
            if symbol:
                conditions.append(table.c.symbol == symbol)

            if conditions:
                query = query.where(and_(*conditions))

            engine = self._ensure_connected()
            with engine.connect() as conn:
                result = conn.execute(query)
                count = result.scalar()
                return count if count is not None else 0

        except Exception as e:
            raise DatabaseError(f"Failed to count records in {collection}: {e}") from e

    def ensure_indexes(self, collection: str) -> None:
        """Ensure indexes exist (handled during table creation)."""
        logger.info("Indexes already exist for table: %s", collection)

    def delete_range(
        self,
        collection: str,
        start: datetime,
        end: datetime,
        symbol: str | None = None,
    ) -> int:
        """Delete records within time range."""
        if not self._connected:
            raise DatabaseError("Not connected to database")

        try:
            table = self._get_table(collection)

            conditions = [table.c.timestamp >= start, table.c.timestamp < end]

            if symbol:
                conditions.append(table.c.symbol == symbol)

            stmt = delete(table).where(and_(*conditions))

            engine = self._ensure_connected()
            with engine.connect() as conn:
                trans = conn.begin()
                try:
                    result = conn.execute(stmt)
                    trans.commit()
                    return result.rowcount
                except Exception:
                    trans.rollback()
                    raise

        except Exception as e:
            raise DatabaseError(f"Failed to delete from {collection}: {e}") from e

    def _ensure_connected(self) -> "Engine":
        """Ensure the database engine is connected and return it."""
        if self.engine is None:
            raise DatabaseError("Database engine is not initialized")
        if not self._connected:
            raise DatabaseError("Database is not connected")
        return self.engine
