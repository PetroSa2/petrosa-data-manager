"""MySQL persistence and replay queries for point-in-time signals."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, func, or_, select, update

from data_manager.db.base_adapter import DatabaseError
from data_manager.models.signal import SignalRecord


def _hash_payload(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class SignalRepository:
    """Keep signal writes and replay reads behind the typed MySQL boundary."""

    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter

    def upsert(self, signal: SignalRecord) -> dict[str, Any]:
        table = self.adapter._get_table("signals")
        if "signal_key" not in table.c or not signal.signal_key:
            raise DatabaseError("signal_key is required for durable signal writes")
        payload = signal.model_dump_for_storage()
        payload_hash = _hash_payload(payload)
        payload["signal_revision_payload_hash"] = payload_hash
        engine = self.adapter._ensure_connected()
        with engine.begin() as conn:
            existing = (
                conn.execute(
                    select(table)
                    .where(table.c.signal_key == signal.signal_key)
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if existing is None:
                conn.execute(
                    table.insert().values(
                        **{
                            key: value
                            for key, value in payload.items()
                            if key in table.c
                        }
                    )
                )
                return {"status": "inserted", "signal_key": signal.signal_key}

            conflicts = [
                key
                for key, value in payload.items()
                if key in table.c
                and key not in {"signal_revision_payload_hash"}
                and value is not None
                and existing.get(key) is not None
                and not _same_value(existing.get(key), value)
            ]
            if conflicts:
                values: dict[str, Any] = {}
                if "last_rejected_payload_hash" in table.c:
                    values["last_rejected_payload_hash"] = payload_hash
                if "signal_revision_conflicts" in table.c:
                    values["signal_revision_conflicts"] = (
                        existing.get("signal_revision_conflicts") or 0
                    ) + 1
                _increment_conflict_metric()
            else:
                values = {
                    key: value
                    for key, value in payload.items()
                    if key in table.c
                    and value is not None
                    and existing.get(key) is None
                }
            if values:
                conn.execute(
                    update(table)
                    .where(table.c.signal_key == signal.signal_key)
                    .values(**values)
                )
            return {
                "status": "conflict" if conflicts else "existing",
                "signal_key": signal.signal_key,
                "conflicting_fields": conflicts,
            }

    def replay(
        self,
        *,
        strategy: str | None,
        symbol: str | None,
        timeframe: str | None,
        from_ts: datetime | None,
        to_ts: datetime | None,
        limit: int,
        offset: int,
        include_legacy: bool,
    ) -> tuple[list[dict[str, Any]], int]:
        table = self.adapter._get_table("signals")
        conditions = []
        if not include_legacy and "signal_key" in table.c:
            conditions.append(table.c.signal_key.is_not(None))
        for name, value in (
            ("strategy", strategy),
            ("symbol", symbol),
            ("timeframe", timeframe),
        ):
            if value and name in table.c:
                conditions.append(table.c[name] == value)
        if from_ts and "bar_open_time" in table.c:
            conditions.append(table.c.bar_open_time >= _as_utc(from_ts))
        if to_ts and "bar_open_time" in table.c:
            conditions.append(table.c.bar_open_time < _as_utc(to_ts))

        execution = self.adapter._get_table("execution_events")
        signal_columns = [
            table.c[name]
            for name in (
                "signal_key",
                "symbol",
                "timeframe",
                "strategy",
                "bar_open_time",
                "bar_close_time",
                "entry_ref_price",
                "stop_loss",
                "take_profit",
                "decision_id",
            )
            if name in table.c
        ]
        execution_columns = [
            execution.c.order_id.label("execution_id"),
            execution.c.event_type.label("fill_event_type"),
            execution.c.fill_qty,
            execution.c.fill_price,
            execution.c.price.label("execution_price"),
            execution.c.pnl,
        ]
        join_condition = and_(
            table.c.decision_id == execution.c.decision_id,
            execution.c.event_type.in_(["filled", "partial_fill"]),
        )
        query = select(*signal_columns, *execution_columns).select_from(
            table.outerjoin(execution, join_condition)
        )
        count_query = select(func.count()).select_from(table)
        if conditions:
            query = query.where(and_(*conditions))
            count_query = count_query.where(and_(*conditions))
        order_columns = []
        if "bar_open_time" in table.c:
            order_columns.append(table.c.bar_open_time.asc())
        if "id" in table.c:
            order_columns.append(table.c.id.asc())
        query = query.order_by(*order_columns).limit(limit).offset(offset)
        engine = self.adapter._ensure_connected()
        with engine.connect() as conn:
            total = int(conn.execute(count_query).scalar() or 0)
            rows = [dict(row) for row in conn.execute(query).mappings()]
        for row in rows:
            row["fill_outcome"] = _fill_outcome(row)
        return rows, total

    def coverage(
        self,
        *,
        strategy: str | None,
        symbol: str | None,
        timeframe: str | None,
        from_ts: datetime | None,
        to_ts: datetime | None,
    ) -> dict[str, Any]:
        rows, total = self.replay(
            strategy=strategy,
            symbol=symbol,
            timeframe=timeframe,
            from_ts=from_ts,
            to_ts=to_ts,
            limit=100_000,
            offset=0,
            include_legacy=False,
        )
        _legacy_rows, legacy_total = self.replay(
            strategy=strategy,
            symbol=symbol,
            timeframe=timeframe,
            from_ts=from_ts,
            to_ts=to_ts,
            limit=100_000,
            offset=0,
            include_legacy=True,
        )
        bars = [
            _as_utc(row["bar_open_time"])
            for row in rows
            if row.get("bar_open_time") is not None
        ]
        present: set[datetime] = set()
        last_kline: datetime | None = None
        if timeframe and symbol:
            klines = self.adapter.query_range(
                f"klines_{timeframe}",
                _as_utc(from_ts) or datetime.min,
                _as_utc(to_ts) or datetime.max,
                symbol=symbol,
                columns=["open_time"],
            )
            present = {
                _as_utc(row.get("open_time")) for row in klines if row.get("open_time")
            }
            last_kline = max(present) if present else None
        missing = [bar for bar in bars if bar not in present]
        return {
            "signal_count": total,
            "klines_present": len(bars) - len(missing),
            "missing_kline_bars": missing,
            "kline_source": "mysql" if timeframe and symbol else "unavailable",
            "last_kline_time": last_kline,
            "legacy_rows": max(0, legacy_total - total),
        }


def _fill_outcome(row: dict[str, Any]) -> dict[str, Any] | None:
    if not row.get("execution_id"):
        return None
    return {
        "event_type": row.pop("fill_event_type", None),
        "fill_qty": row.pop("fill_qty", None),
        "fill_price": row.pop("fill_price", None),
        "price": row.pop("execution_price", None),
        "pnl": row.pop("pnl", None),
    }


def _same_value(left: Any, right: Any) -> bool:
    if isinstance(left, datetime) and isinstance(right, datetime):
        return _as_utc(left) == _as_utc(right)
    return left == right


def _increment_conflict_metric() -> None:
    try:
        from data_manager.api.routes.signals import signal_revision_conflicts_total

        signal_revision_conflicts_total.inc()
    except Exception:
        pass
