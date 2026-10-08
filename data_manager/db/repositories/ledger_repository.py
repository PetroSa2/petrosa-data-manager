"""Insert-only MySQL repository for exchange ledger snapshots."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from data_manager.db.repositories.base_repository import BaseRepository
from data_manager.services.ledger_tolerance import (
    FALLBACK_CUMULATIVE,
    ExchangeInfoCache,
    calculate_tolerance,
    fallback_limits,
)

_exchange_info = ExchangeInfoCache()


class LedgerRepository(BaseRepository):
    """Persist ledger revisions without update or delete methods."""

    def _run(self, statement: str, params: dict[str, Any] | None = None):
        if self.mysql is None:
            raise RuntimeError("MySQL is not available")
        engine = self.mysql._ensure_connected()
        with engine.begin() as connection:
            return connection.execute(text(statement), params or {})

    @staticmethod
    def economic_hash(payload: dict[str, Any]) -> str:
        economic = {
            "rows": payload["rows"],
            "row_count": payload["row_count"],
            "last_income_time_ms": payload.get("last_income_time_ms"),
            "is_final": payload["is_final"],
        }
        encoded = json.dumps(economic, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()

    def latest_day(self, day: str) -> dict[str, Any] | None:
        result = (
            self._run(
                "SELECT * FROM ledger_exchange_day_revision "
                "WHERE day=:day ORDER BY revision DESC LIMIT 1",
                {"day": day},
            )
            .mappings()
            .first()
        )
        return dict(result) if result else None

    def next_revision(self, day: str) -> int:
        result = self._run(
            "SELECT COALESCE(MAX(revision), 0) + 1 AS revision "
            "FROM ledger_exchange_day_revision WHERE day=:day",
            {"day": day},
        ).scalar_one()
        return int(result)

    def insert_day(
        self, payload: dict[str, Any], received_at: datetime
    ) -> dict[str, Any]:
        day = payload["day"]
        payload_hash = self.economic_hash(payload)
        latest = self.latest_day(day)
        if latest and latest["payload_hash"] == payload_hash:
            return {
                "day": day,
                "revision": int(latest["revision"]),
                "created": False,
                "restated": False,
            }
        revision = self.next_revision(day)
        self._run(
            "INSERT INTO ledger_exchange_day_revision "
            "(day, revision, wallet_balance, balance_as_of_ms, income_after_day_end, "
            "is_final, row_count, first_income_time_ms, last_income_time_ms, payload_hash, "
            "source_run_id, received_at) VALUES "
            "(:day,:revision,:wallet_balance,:balance_as_of_ms,:income_after_day_end,"
            ":is_final,:row_count,:first_income_time_ms,:last_income_time_ms,:payload_hash,"
            ":source_run_id,:received_at)",
            {
                "day": day,
                "revision": revision,
                "wallet_balance": Decimal(payload["wallet_balance"]),
                "balance_as_of_ms": payload.get("balance_as_of_ms"),
                "income_after_day_end": json.dumps(
                    payload.get("income_after_day_end", {}), sort_keys=True
                ),
                "is_final": payload["is_final"],
                "row_count": payload["row_count"],
                "first_income_time_ms": payload.get("first_income_time_ms"),
                "last_income_time_ms": payload.get("last_income_time_ms"),
                "payload_hash": payload_hash,
                "source_run_id": payload["source_run_id"],
                "received_at": received_at,
            },
        )
        columns = (
            "realized_pnl",
            "commission",
            "funding_fee",
            "transfer",
            "commission_rebate",
            "api_rebate",
            "insurance_clear",
            "auto_exchange",
            "other_unnamed",
        )
        for row in payload["rows"]:
            values = {name: Decimal("0") for name in columns}
            for income_type, amount in row["income_by_type"].items():
                values[
                    {
                        "REALIZED_PNL": "realized_pnl",
                        "COMMISSION": "commission",
                        "FUNDING_FEE": "funding_fee",
                        "TRANSFER": "transfer",
                        "COMMISSION_REBATE": "commission_rebate",
                        "API_REBATE": "api_rebate",
                        "INSURANCE_CLEAR": "insurance_clear",
                        "AUTO_EXCHANGE": "auto_exchange",
                    }.get(income_type, "other_unnamed")
                ] += Decimal(amount)
            self._run(
                "INSERT INTO ledger_exchange_daily "
                "(day,revision,symbol,asset,realized_pnl,commission,funding_fee,transfer,"
                "commission_rebate,api_rebate,insurance_clear,auto_exchange,other_unnamed,"
                "income_by_type) VALUES (:day,:revision,:symbol,:asset,:realized_pnl,:commission,"
                ":funding_fee,:transfer,:commission_rebate,:api_rebate,:insurance_clear,"
                ":auto_exchange,:other_unnamed,:income_by_type)",
                {
                    "day": day,
                    "revision": revision,
                    "symbol": row.get("symbol") or "",
                    "asset": row["asset"],
                    **values,
                    "income_by_type": json.dumps(row["income_by_type"], sort_keys=True),
                },
            )
        if latest and latest["is_final"]:
            self._run(
                "INSERT INTO ledger_exchange_metrics (metric, value) VALUES "
                "('ledger_final_day_restated_total', 1) ON DUPLICATE KEY UPDATE value=value+1"
            )
        return {
            "day": day,
            "revision": revision,
            "created": True,
            "restated": bool(latest and latest["is_final"]),
        }

    def put_positions(
        self, payload: dict[str, Any], received_at: datetime
    ) -> dict[str, Any]:
        content = {"rows": payload["rows"]}
        digest = hashlib.sha256(
            json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        latest = (
            self._run(
                "SELECT * FROM ledger_exchange_positions_snapshot ORDER BY as_of_ms DESC LIMIT 1"
            )
            .mappings()
            .first()
        )
        heartbeat = int(os.getenv("LEDGER_POSITIONS_HEARTBEAT_SECONDS", "60"))
        if (
            latest
            and latest["payload_hash"] == digest
            and int(payload["as_of_ms"]) - int(latest["as_of_ms"]) < heartbeat * 1000
        ):
            return {"as_of_ms": payload["as_of_ms"], "created": False}
        self._run(
            "INSERT INTO ledger_exchange_positions_snapshot "
            "(as_of_ms,position_count,source_run_id,payload_hash,received_at) "
            "VALUES (:as_of_ms,:position_count,:source_run_id,:payload_hash,:received_at)",
            {
                "as_of_ms": payload["as_of_ms"],
                "position_count": len(payload["rows"]),
                "source_run_id": payload["source_run_id"],
                "payload_hash": digest,
                "received_at": received_at,
            },
        )
        for row in payload["rows"]:
            self._run(
                "INSERT INTO ledger_exchange_positions "
                "(as_of_ms,symbol,position_side,quantity,entry_price,mark_price,unrealized_pnl) "
                "VALUES (:as_of_ms,:symbol,:position_side,:quantity,:entry_price,:mark_price,:unrealized_pnl)",
                {
                    "as_of_ms": payload["as_of_ms"],
                    **{
                        k: Decimal(row[k])
                        for k in (
                            "quantity",
                            "entry_price",
                            "mark_price",
                            "unrealized_pnl",
                        )
                    },
                    "symbol": row["symbol"],
                    "position_side": row["position_side"],
                },
            )
        return {"as_of_ms": payload["as_of_ms"], "created": True}

    def tieout(self, first: date, last: date) -> dict[str, Any]:
        """Build a bounded daily comparison from the latest exchange revisions."""
        days = (
            self._run(
                "SELECT r.*, COALESCE(SUM(e.realized_pnl),0) realized_pnl, COALESCE(SUM(e.commission),0) commission, "
                "COALESCE(SUM(e.funding_fee),0) funding_fee, COALESCE(SUM(e.transfer+e.commission_rebate+e.api_rebate+e.insurance_clear+e.auto_exchange+e.other_unnamed),0) other_income_total "
                "FROM ledger_exchange_day_revision r JOIN (SELECT day, MAX(revision) revision FROM ledger_exchange_day_revision WHERE day BETWEEN :first AND :last GROUP BY day) latest ON latest.day=r.day AND latest.revision=r.revision "
                "LEFT JOIN ledger_exchange_daily e ON e.day=r.day AND e.revision=r.revision GROUP BY r.day,r.revision ORDER BY r.day",
                {"first": first, "last": last},
            )
            .mappings()
            .all()
        )
        pnl = (
            self._run(
                "SELECT date, daily_pnl FROM daily_pnl WHERE date BETWEEN :first AND :last",
                {"first": first, "last": last},
            )
            .mappings()
            .all()
        )
        pnl_by_day = {
            row["date"].isoformat(): Decimal(str(row["daily_pnl"])) for row in pnl
        }
        fills_by_day: dict[str, list[dict[str, Any]]] = {}
        try:
            fills = (
                self._run(
                    "SELECT symbol, quantity, commission, commission_asset, trade_time "
                    "FROM trades WHERE trade_time >= :first AND trade_time < :after",
                    {
                        "first": datetime.combine(first, datetime.min.time()),
                        "after": datetime.combine(
                            last + timedelta(days=1), datetime.min.time()
                        ),
                    },
                )
                .mappings()
                .all()
            )
        except IndexError:
            fills_by_day = {}
        else:
            for fill in fills:
                item = dict(fill)
                symbol = item.get("symbol")
                try:
                    filters = _exchange_info.get(symbol) if symbol else None
                except (OSError, ValueError):
                    filters = None
                if filters:
                    item.update(filters)
                fills_by_day.setdefault(
                    item["trade_time"].date().isoformat(), []
                ).append(item)
        result, cumulative = [], Decimal("0")
        trailing_variances: list[Decimal] = []
        fallback = fallback_limits()
        known = {row["day"].isoformat(): row for row in days}
        for offset in range((last - first).days + 1):
            day = first + timedelta(days=offset)
            key, row = day.isoformat(), known.get(day.isoformat())
            exchange = (
                Decimal(str(row["realized_pnl"])) + Decimal(str(row["commission"]))
                if row
                else Decimal("0")
            )
            ledger = pnl_by_day.get(key)
            variance = ledger - exchange if ledger is not None else Decimal("0")
            cumulative += variance
            tolerance = calculate_tolerance(
                (row.get("fills", []) if row else []) + fills_by_day.get(key, []),
                trailing_variances,
                fallback=Decimal(fallback["daily"]),
            )
            day_fills = (row.get("fills", []) if row else []) + fills_by_day.get(
                key, []
            )
            fills_by_symbol: dict[str, list[dict[str, Any]]] = {}
            for fill in day_fills:
                symbol = str(fill.get("symbol", "unknown"))
                fills_by_symbol.setdefault(symbol, []).append(fill)
            symbol_tolerances = {
                symbol: calculate_tolerance(
                    symbol_fills,
                    trailing_variances,
                    fallback=Decimal(fallback["daily"]),
                )
                for symbol, symbol_fills in fills_by_symbol.items()
            }
            cumulative_samples = (trailing_variances + [variance])[-30:]
            cumulative_tolerance = max(
                FALLBACK_CUMULATIVE,
                Decimal(
                    calculate_tolerance(
                        [], cumulative_samples, fallback=FALLBACK_CUMULATIVE
                    )["amount"]
                ),
            )
            status = (
                "ledger_missing"
                if ledger is None and exchange
                else ("tied" if variance == 0 else "unconfigured")
            )
            is_break = abs(variance) > Decimal(tolerance["amount"])
            if is_break:
                status = "break"
            result.append(
                {
                    "day": key,
                    "realized_and_fees": {
                        "exchange": str(exchange),
                        "ledger": str(ledger) if ledger is not None else None,
                        "variance": str(variance),
                    },
                    "funding": {
                        "exchange": str(row["funding_fee"]) if row else "0",
                        "ledger": None,
                        "status": "unbooked_by_design",
                    },
                    "other_income_total": str(row["other_income_total"])
                    if row
                    else "0",
                    "unexplained": str(variance),
                    "cumulative_variance": str(cumulative),
                    "tolerance": {
                        "daily": tolerance,
                        "symbol_days": symbol_tolerances,
                        "cumulative": {
                            "amount": str(cumulative_tolerance),
                            "source": (
                                "source: variance"
                                if len(cumulative_samples) >= 30
                                else "source: fallback"
                            ),
                        },
                        "per_item": {
                            "amount": fallback["per_item"],
                            "source": "source: fallback",
                        },
                    },
                    "break": is_break,
                    "status": status,
                    "roll_forward": {
                        "difference_class": "provisional"
                        if row and not row["is_final"]
                        else "opening_missing"
                    },
                }
            )
            trailing_variances.append(variance)
        return {
            "from": first.isoformat(),
            "to": last.isoformat(),
            "days": result,
            "cumulative_variance": str(cumulative),
        }

    def positions_tieout(self) -> dict[str, Any]:
        """Compare latest exchange positions with open historic rows."""
        snapshot = (
            self._run(
                "SELECT * FROM ledger_exchange_positions_snapshot ORDER BY as_of_ms DESC LIMIT 1"
            )
            .mappings()
            .first()
        )
        if not snapshot:
            return {
                "ledger_open_rows": [],
                "exchange_positions": [],
                "phantom_rows": [],
            }
        exchange = (
            self._run(
                "SELECT symbol, position_side, quantity FROM ledger_exchange_positions WHERE as_of_ms=:as_of_ms",
                {"as_of_ms": snapshot["as_of_ms"]},
            )
            .mappings()
            .all()
        )
        ledger = (
            self._run(
                "SELECT symbol, position_side, quantity FROM positions WHERE status IN ('open','partially_closed')"
            )
            .mappings()
            .all()
        )
        exchange_keys = {(r["symbol"], r["position_side"]): r for r in exchange}
        grouped: dict[tuple[str, str], int] = {}
        for row in ledger:
            side = {"BUY": "LONG", "SELL": "SHORT"}.get(
                str(row["position_side"]).upper(), str(row["position_side"]).upper()
            )
            grouped[(row["symbol"], side)] = grouped.get((row["symbol"], side), 0) + 1
        open_rows = [
            {"symbol": s, "position_side": side, "ledger_open_rows": count}
            for (s, side), count in grouped.items()
        ]
        exchange_rows = [
            {
                "symbol": s,
                "position_side": side,
                "exchange_positions": 1,
                "quantity": str(r["quantity"]),
            }
            for (s, side), r in exchange_keys.items()
        ]
        phantom = [
            row
            for row in open_rows
            if (row["symbol"], row["position_side"]) not in exchange_keys
        ]
        return {
            "as_of_ms": snapshot["as_of_ms"],
            "ledger_open_rows": open_rows,
            "exchange_positions": exchange_rows,
            "phantom_rows": phantom,
        }
