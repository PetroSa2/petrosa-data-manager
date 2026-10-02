"""Repository tests for durable signal identity and replay behavior."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import sqlalchemy as sa

from data_manager.db.repositories.signal_repository import SignalRepository
from data_manager.models.signal import SignalRecord


def _adapter():
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    signals = sa.Table(
        "signals",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("symbol", sa.String),
        sa.Column("timeframe", sa.String),
        sa.Column("strategy", sa.String),
        sa.Column("signal_type", sa.String),
        sa.Column("period", sa.String),
        sa.Column("confidence", sa.Float),
        sa.Column("metadata", sa.JSON),
        sa.Column("timestamp", sa.DateTime),
        sa.Column("signal_key", sa.String, unique=True),
        sa.Column("bar_open_time", sa.DateTime),
        sa.Column("bar_close_time", sa.DateTime),
        sa.Column("entry_ref_price", sa.Float),
        sa.Column("stop_loss", sa.Float),
        sa.Column("take_profit", sa.Float),
        sa.Column("decision_id", sa.String),
        sa.Column("signal_revision_payload_hash", sa.String),
        sa.Column("last_rejected_payload_hash", sa.String),
        sa.Column("signal_revision_conflicts", sa.Integer, default=0),
    )
    execution = sa.Table(
        "execution_events",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("decision_id", sa.String),
        sa.Column("order_id", sa.String),
        sa.Column("event_type", sa.String),
        sa.Column("fill_qty", sa.Float),
        sa.Column("fill_price", sa.Float),
        sa.Column("price", sa.Float),
        sa.Column("pnl", sa.Float),
    )
    klines = sa.Table(
        "klines_5m",
        metadata,
        sa.Column("symbol", sa.String),
        sa.Column("open_time", sa.DateTime),
    )
    metadata.create_all(engine)

    def query_range(collection, start, end, symbol=None, columns=None):
        table = {"klines_5m": klines}[collection]
        start = start.replace(tzinfo=None)
        end = end.replace(tzinfo=None)
        query = sa.select(*[table.c[name] for name in columns]).where(
            table.c.open_time >= start, table.c.open_time < end
        )
        if symbol:
            query = query.where(table.c.symbol == symbol)
        with engine.connect() as conn:
            return [dict(row) for row in conn.execute(query).mappings()]

    return SimpleNamespace(
        engine=engine,
        tables={"signals": signals, "execution_events": execution, "klines_5m": klines},
        _get_table=lambda name: {"signals": signals, "execution_events": execution, "klines_5m": klines}[name],
        _ensure_connected=lambda: engine,
        query_range=query_range,
    )


def _signal(key="key-1", **values):
    defaults = {
        "symbol": "BTCUSDT",
        "timeframe": "5m",
        "strategy": "test",
        "signal_key": key,
        "bar_open_time": datetime(2026, 1, 1, tzinfo=UTC),
    }
    defaults.update(values)
    return SignalRecord(**defaults)


def test_upsert_is_idempotent_and_conflicts_are_first_write_wins():
    adapter = _adapter()
    repository = SignalRepository(adapter)
    assert repository.upsert(_signal(entry_ref_price=10))["status"] == "inserted"
    assert repository.upsert(_signal(entry_ref_price=10))["status"] == "existing"
    conflict = repository.upsert(_signal(entry_ref_price=11))
    assert conflict["status"] == "conflict"
    with adapter.engine.connect() as conn:
        row = conn.execute(sa.select(adapter.tables["signals"])).mappings().one()
    assert row["entry_ref_price"] == 10
    assert row["signal_revision_conflicts"] == 1
    assert row["last_rejected_payload_hash"]


def test_replay_joins_fill_and_excludes_legacy_by_default():
    adapter = _adapter()
    repository = SignalRepository(adapter)
    repository.upsert(_signal(decision_id="decision-1"))
    with adapter.engine.begin() as conn:
        conn.execute(
            adapter.tables["execution_events"].insert().values(
                decision_id="decision-1", order_id="order-1", event_type="filled",
                fill_qty=2, fill_price=12, price=12, pnl=3,
            )
        )
        conn.execute(
            adapter.tables["signals"].insert().values(
                symbol="ETHUSDT", timeframe="5m", strategy="legacy",
                signal_type="buy", period="5m", confidence=1,
                metadata={}, timestamp=datetime.now(UTC),
            )
        )
    rows, total = repository.replay(
        strategy=None, symbol=None, timeframe="5m", from_ts=None, to_ts=None,
        limit=10, offset=0, include_legacy=False,
    )
    assert total == 1
    assert rows[0]["execution_id"] == "order-1"
    assert rows[0]["fill_outcome"]["pnl"] == 3
    _, legacy_total = repository.replay(
        strategy=None, symbol=None, timeframe="5m", from_ts=None, to_ts=None,
        limit=10, offset=0, include_legacy=True,
    )
    assert legacy_total == 2


def test_coverage_reports_missing_kline_and_legacy_count():
    adapter = _adapter()
    repository = SignalRepository(adapter)
    repository.upsert(_signal(key="signal-1"))
    repository.upsert(_signal(key="signal-2", bar_open_time=datetime(2026, 1, 1, 0, 5, tzinfo=UTC)))
    with adapter.engine.begin() as conn:
        conn.execute(
            adapter.tables["klines_5m"].insert().values(
                symbol="BTCUSDT", open_time=datetime(2026, 1, 1, tzinfo=UTC)
            )
        )
    result = repository.coverage(
        strategy="test", symbol="BTCUSDT", timeframe="5m",
        from_ts=datetime(2026, 1, 1, tzinfo=UTC),
        to_ts=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=1),
    )
    assert result["signal_count"] == 2
    assert result["klines_present"] == 1
    assert len(result["missing_kline_bars"]) == 1
    assert result["legacy_rows"] == 0
