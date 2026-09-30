"""Insert-only MySQL repository for exchange ledger snapshots."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from data_manager.db.repositories.base_repository import BaseRepository


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
