"""
MySQL adapter implementation for Data Manager.

Based on petrosa-binance-data-extractor patterns.
"""

import logging
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel

try:
    import sqlalchemy as sa
    from sqlalchemy import (
        Column,
        DateTime,
        Enum,
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
from data_manager.utils.retry import retry_transient

logger = logging.getLogger(__name__)


class WriteResult(int):
    """Outcome of a MySQL write — explicit ``inserted`` / ``duplicates`` / ``failed`` counts.

    Subclasses ``int`` so existing callers / tests that compare the return value
    against an integer (e.g. ``assert adapter.write(...) == 0``) keep working;
    the integer value is the number of rows actually inserted. New callers can
    read ``.inserted``, ``.duplicates``, and ``.failed`` directly — required by
    petrosa-data-manager#213 AC2.3 so the boundary stops returning an ambiguous
    plain ``0`` on the all-duplicates case.
    """

    inserted: int
    duplicates: int
    failed: int

    def __new__(
        cls, inserted: int = 0, duplicates: int = 0, failed: int = 0
    ) -> "WriteResult":
        obj = super().__new__(cls, inserted)
        obj.inserted = inserted
        obj.duplicates = duplicates
        obj.failed = failed
        return obj

    def as_dict(self) -> dict[str, int]:
        return {
            "inserted": self.inserted,
            "duplicates": self.duplicates,
            "failed": self.failed,
        }


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
            Index("idx_audit_logs_symbol_timestamp", "symbol", "timestamp"),
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
            Index("idx_health_metrics_symbol_timestamp", "symbol", "timestamp"),
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
            Column(
                "status",
                Enum("pending", "running", "completed", "failed"),
                nullable=False,
            ),
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

    def _create_klines_table(
        self, interval: str, collection_name: str | None = None
    ) -> "Table":
        """Create or reflect a klines table for specific interval."""
        # Map binance interval to table suffix (e.g., 1h -> h1)
        suffix = interval
        if interval.endswith("m"):
            suffix = f"m{interval[:-1]}"
        elif interval.endswith("h"):
            suffix = f"h{interval[:-1]}"
        elif interval.endswith("d"):
            suffix = f"d{interval[:-1]}"

        physical_table_name = f"klines_{suffix}"
        # Use requested collection name as the Table object name if provided
        object_name = collection_name or physical_table_name

        if object_name in self.tables:
            return self.tables[object_name]

        table = Table(
            physical_table_name,
            self.metadata,
            Column("id", String(64), primary_key=True),
            Column("symbol", String(20), nullable=False),
            Column("timestamp", DateTime, nullable=False),
            Column("open_time", DateTime, nullable=False),
            Column("close_time", DateTime, nullable=False),
            Column("interval", String(10), nullable=False),
            Column("open_price", Numeric(20, 8), nullable=False),
            Column("high_price", Numeric(20, 8), nullable=False),
            Column("low_price", Numeric(20, 8), nullable=False),
            Column("close_price", Numeric(20, 8), nullable=False),
            Column("volume", Numeric(20, 8), nullable=False),
            Column("quote_asset_volume", Numeric(20, 8), nullable=False),
            Column("number_of_trades", Integer, nullable=False),
            Column("taker_buy_base_asset_volume", Numeric(20, 8), nullable=False),
            Column("taker_buy_quote_asset_volume", Numeric(20, 8), nullable=False),
            Column("price_change", Numeric(20, 8), nullable=True),
            Column("price_change_percent", Numeric(10, 4), nullable=True),
            Column("extracted_at", DateTime, nullable=False),
            Column("extractor_version", String(20), nullable=False),
            Column("source", String(50), nullable=False),
            Index(f"idx_{physical_table_name}_symbol_timestamp", "symbol", "timestamp"),
            Index(f"idx_{physical_table_name}_timestamp", "timestamp"),
            extend_existing=True,
        )

        self.tables[object_name] = table
        if self.engine is not None:
            table.create(self.engine, checkfirst=True)
            logger.info(
                f"Created/Verified table: {physical_table_name} (mapped to {object_name})"
            )
        return table

    def _get_table(self, collection: str) -> "Table":
        """Get table object for collection. Dynamically create or reflect."""
        if collection in self.tables:
            return self.tables[collection]

        # Handle klines tables specially (e.g., klines_1h -> maps to physical klines_h1)
        if collection.startswith("klines_"):
            suffix = collection.replace("klines_", "")
            interval = suffix
            # Detect if it's already in financial style (e.g. h1) or binance style (e.g. 1h)
            if any(suffix.startswith(x) for x in ["m", "h", "d"]):
                # Already financial style (m15, h1, etc.)
                if suffix.startswith("m"):
                    interval = f"{suffix[1:]}m"
                elif suffix.startswith("h"):
                    interval = f"{suffix[1:]}h"
                elif suffix.startswith("d"):
                    interval = f"{suffix[1:]}d"

            return self._create_klines_table(interval, collection_name=collection)

        # Try to reflect the table dynamically if it exists in the database
        try:
            engine = self._ensure_connected()
            # Check if table already exists in metadata before creating new Table object
            if collection in self.metadata.tables:
                table = self.metadata.tables[collection]
            else:
                table = Table(collection, self.metadata, autoload_with=engine)

            self.tables[collection] = table
            logger.info(f"Dynamically reflected table: {collection}")
            return table
        except Exception as e:
            logger.error(f"Failed to reflect table {collection}: {e}")
            raise DatabaseError(
                f"Unknown collection or failed to reflect: {collection}"
            )

    def write(self, model_instances: list[BaseModel], collection: str) -> WriteResult:
        """Write model instances to MySQL with retry + circuit breaker.

        Returns a :class:`WriteResult` carrying explicit ``inserted`` /
        ``duplicates`` / ``failed`` counts so callers can distinguish
        "all-duplicates" from "wrote nothing because it failed" (resolves
        petrosa-data-manager#213 AC2.3). ``WriteResult`` is an ``int``
        subclass valued at ``inserted``, preserving backward compatibility
        for callers that compare the result against a plain integer.

        Retry semantics (per #213 F1):
            * Transient errors (lost connection, deadlock, lock-wait timeout)
              are retried with backoff INSIDE the circuit breaker call. Each
              *exhausted* retry cycle counts as one breaker failure — the
              breaker still opens after ``failure_threshold`` exhausted
              cycles, not 5×3 raw underlying errors.
            * ``IntegrityError`` is NOT retried (#213 AC2.2). Note: with
              ``INSERT IGNORE`` MySQL downgrades duplicate-key collisions to
              warnings and reduces ``rowcount`` instead of raising — so this
              path almost never fires for duplicates (#213 F2). It still
              catches genuine integrity faults (FK violation, NOT NULL).
        """
        if not self._connected:
            raise DatabaseError("Not connected to database")

        if not model_instances:
            return WriteResult(0, 0, 0)

        # Prepare records once outside the retry loop so a retry never
        # mutates a payload that the previous attempt already normalized.
        table = self._get_table(collection)
        records: list[dict[str, Any]] = []
        for instance in model_instances:
            record = instance.model_dump()
            if "id" in table.c and ("id" not in record or not record["id"]):
                record["id"] = str(uuid.uuid4())
            for key, value in record.items():
                if isinstance(value, str) and key.endswith(("_at", "timestamp")):
                    try:
                        record[key] = datetime.fromisoformat(value)
                    except (ValueError, TypeError):
                        pass
            records.append(record)
        total = len(records)

        # klines tables carry `extracted_at`; use ON DUPLICATE KEY UPDATE to
        # avoid MySQL 5.x gap-lock contention that INSERT IGNORE causes on
        # unique-index conflicts (petrosa-data-manager#231).
        uses_on_dup_key = "extracted_at" in table.c

        def _write_attempt() -> int:
            """Single write attempt — returns rowcount or raises."""
            from sqlalchemy.dialects.mysql import insert as mysql_insert

            engine = self._ensure_connected()
            with engine.connect() as conn:
                trans = conn.begin()
                try:
                    if uses_on_dup_key:
                        ins = mysql_insert(table)
                        stmt = ins.on_duplicate_key_update(
                            extracted_at=ins.inserted.extracted_at
                        )
                    else:
                        stmt = table.insert().prefix_with("IGNORE")
                    result = conn.execute(stmt, records)
                    trans.commit()
                    return int(result.rowcount or 0)
                except Exception:
                    trans.rollback()
                    raise

        def _write_with_retry() -> int:
            try:
                return retry_transient(_write_attempt)
            except IntegrityError:
                # Non-transient: FK violation / NOT NULL / similar.
                # ON DUPLICATE KEY UPDATE and INSERT IGNORE both absorb
                # duplicate-key collisions without raising here.
                logger.warning(
                    "Integrity error writing to %s — not a duplicate (absorbed by upsert/ignore)",
                    collection,
                )
                raise
            except DatabaseError:
                raise
            except Exception as exc:
                raise DatabaseError(f"Error in MySQL write: {exc}") from exc

        try:
            rowcount = self.circuit_breaker.call(_write_with_retry)
        except IntegrityError:
            self._record_write_failure(collection, "integrity_error")
            return WriteResult(inserted=0, duplicates=0, failed=total)
        except DatabaseError:
            self._record_write_failure(collection, "database_error")
            raise
        except Exception:
            # CircuitBreakerOpenError + truly unexpected — let the HTTP
            # boundary classify; still count the failure for observability.
            self._record_write_failure(collection, "circuit_or_unknown")
            raise

        if uses_on_dup_key:
            # ON DUPLICATE KEY UPDATE: MySQL reports 1 per insert, 2 per update.
            # duplicates = rowcount - total; inserts = total - duplicates.
            duplicates = max(0, rowcount - total)
        else:
            # INSERT IGNORE: MySQL reports 1 per insert, 0 per ignored duplicate.
            duplicates = max(0, total - rowcount)
        inserts = total - duplicates
        return WriteResult(inserted=inserts, duplicates=duplicates, failed=0)

    def _record_write_failure(self, collection: str, reason: str) -> None:
        """Increment the write-failure counter — fire-and-forget; never raise."""
        try:
            from data_manager.api.middleware import metrics as _metrics

            counter = getattr(_metrics, "MYSQL_WRITE_FAILURES", None)
            if counter is not None:
                counter.labels(
                    database="mysql", collection=collection, reason=reason
                ).inc()
        except Exception:  # pragma: no cover - metrics must never break writes
            logger.debug("Failed to record write-failure metric", exc_info=True)

    def write_batch(
        self, model_instances: list[BaseModel], collection: str, batch_size: int = 1000
    ) -> WriteResult:
        """Write model instances in batches, aggregating per-batch counts."""
        inserted = 0
        duplicates = 0
        failed = 0

        for i in range(0, len(model_instances), batch_size):
            batch = model_instances[i : i + batch_size]
            result = self.write(batch, collection)
            inserted += result.inserted
            duplicates += result.duplicates
            failed += result.failed

            if i + batch_size < len(model_instances):
                logger.debug(
                    "Written batch %d: %d inserted / %d duplicates / %d failed to %s",
                    i // batch_size + 1,
                    result.inserted,
                    result.duplicates,
                    result.failed,
                    collection,
                )

        return WriteResult(inserted=inserted, duplicates=duplicates, failed=failed)

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


# ---------------------------------------------------------------------------
# Read-only introspection helpers (petrosa_k8s#794 storage-inventory audit)
#
# These module-level helpers DELIBERATELY bypass ``MySQLAdapter.connect()``
# because that method runs ``_create_tables()`` → ``metadata.create_all`` —
# DDL that would mutate the live schema before the audit issues its first
# read. The storage-inventory module imports these directly and never calls
# ``MySQLAdapter.connect()``; together with a ``SELECT``-only DB credential,
# this is the two-layer read-only guarantee from petrosa_k8s#794 AC2/AC10.
#
# Excluded from `information_schema` enumeration: ``mysql``, ``information_schema``,
# ``performance_schema``, ``sys`` — server-internal schemas the audit must not
# touch.
# ---------------------------------------------------------------------------

_SYSTEM_SCHEMAS = frozenset(
    {"mysql", "information_schema", "performance_schema", "sys"}
)


def create_read_only_engine(connection_string: str) -> "Engine":
    """Build a bare SQLAlchemy engine that issues NO DDL on creation.

    Unlike :meth:`MySQLAdapter.connect`, this helper does NOT call
    ``_create_tables`` / ``metadata.create_all``. It only constructs the
    engine. The caller is responsible for using a ``SELECT``-only DB
    credential — that is the real safety guarantee; this function is the
    code-level half (no DDL emitted by the data-manager codebase).
    """
    if not SQLALCHEMY_AVAILABLE:
        raise ImportError(
            "SQLAlchemy and MySQL driver required. "
            "Install: pip install sqlalchemy pymysql"
        )
    return create_engine(
        connection_string,
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=2,
        pool_timeout=30,
        connect_args={"charset": "utf8mb4", "autocommit": True},
    )


def list_schemas(engine: "Engine") -> list[str]:
    """Return non-system schema names via ``information_schema.SCHEMATA``."""
    stmt = sa.text(
        "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA ORDER BY SCHEMA_NAME"
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).fetchall()
    return [r[0] for r in rows if r[0] not in _SYSTEM_SCHEMAS]


def table_inventory(engine: "Engine", schema: str) -> list[dict[str, Any]]:
    """Return per-table size info for ``schema`` via ``information_schema.TABLES``.

    Each row carries ``TABLE_TYPE`` so callers can distinguish ``BASE TABLE``
    from ``VIEW`` and avoid double-counting view bytes in storage totals
    (petrosa_k8s#794 AC6). ``TABLE_ROWS`` is an InnoDB estimate — callers
    should label it approximate; ``DATA_LENGTH``/``INDEX_LENGTH`` are the
    storage verdict.
    """
    stmt = sa.text(
        """
        SELECT TABLE_NAME, TABLE_TYPE, TABLE_ROWS,
               COALESCE(DATA_LENGTH, 0) AS DATA_LENGTH,
               COALESCE(INDEX_LENGTH, 0) AS INDEX_LENGTH,
               CREATE_TIME, UPDATE_TIME
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = :schema
        ORDER BY TABLE_NAME
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt, {"schema": schema}).mappings().fetchall()
    return [dict(r) for r in rows]
