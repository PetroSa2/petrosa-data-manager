"""Typed, append-only exchange ledger ingestion routes."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, HTTPException, Query
from prometheus_client import Counter, Gauge
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

import data_manager.api.app as api_module
from data_manager.db.repositories.ledger_repository import LedgerRepository

router = APIRouter(prefix="/api/v1/ledger")
TIEOUT_VARIANCE = Gauge(
    "ledger_tieout_variance_usd", "Latest ledger tie-out variance", ["window"]
)
TIEOUT_CUMULATIVE = Gauge(
    "ledger_tieout_cumulative_variance_usd", "Cumulative ledger tie-out variance"
)
OPEN_POSITION_VARIANCE = Gauge(
    "ledger_open_positions_variance", "Ledger rows without exchange positions"
)
EXCEEDING_DAYS = Counter(
    "ledger_tieout_days_exceeding_total", "Tie-out days exceeding materiality"
)
KNOWN_TYPES = {
    "REALIZED_PNL",
    "COMMISSION",
    "FUNDING_FEE",
    "TRANSFER",
    "COMMISSION_REBATE",
    "API_REBATE",
    "INSURANCE_CLEAR",
    "AUTO_EXCHANGE",
}


def _decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("amount must be a decimal string") from exc
    if not parsed.is_finite():
        raise ValueError("amount must be finite")
    return parsed


class IncomeRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: StrictStr = ""
    asset: StrictStr
    income_by_type: dict[StrictStr, StrictStr]

    @field_validator("income_by_type")
    @classmethod
    def validate_amounts(cls, value):
        for amount in value.values():
            _decimal(amount)
        return value


class DailyLedger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    day: date
    source_run_id: StrictStr
    is_final: bool
    row_count: StrictInt = Field(ge=0)
    first_income_time_ms: StrictInt | None = None
    last_income_time_ms: StrictInt | None = None
    rows: list[IncomeRow]
    wallet_balance: StrictStr
    balance_as_of_ms: StrictInt
    income_after_day_end: dict[StrictStr, StrictStr]

    @field_validator("wallet_balance")
    @classmethod
    def validate_wallet(cls, value):
        _decimal(value)
        return value

    @field_validator("income_after_day_end")
    @classmethod
    def validate_after_day(cls, value):
        for amount in value.values():
            _decimal(amount)
        return value

    @model_validator(mode="after")
    def validate_totals(self):
        if self.row_count != len(self.rows):
            raise ValueError("row_count must equal rows length")
        for row in self.rows:
            total = sum(
                (_decimal(v) for v in row.income_by_type.values()), Decimal("0")
            )
            named = sum(
                (
                    _decimal(v)
                    for k, v in row.income_by_type.items()
                    if k in KNOWN_TYPES
                ),
                Decimal("0"),
            )
            if total != named:
                raise ValueError("income_by_type total is inconsistent")
        return self


class PositionRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: StrictStr
    position_side: StrictStr
    quantity: StrictStr
    entry_price: StrictStr
    mark_price: StrictStr
    unrealized_pnl: StrictStr

    @model_validator(mode="after")
    def validate_amounts(self):
        for value in (
            self.quantity,
            self.entry_price,
            self.mark_price,
            self.unrealized_pnl,
        ):
            _decimal(value)
        if self.position_side not in {"LONG", "SHORT", "BOTH"}:
            raise ValueError("position_side must be LONG, SHORT, or BOTH")
        return self


class PositionsSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    as_of_ms: StrictInt
    source_run_id: StrictStr
    rows: list[PositionRow] = Field(default_factory=list)


def _repo() -> LedgerRepository:
    manager = api_module.db_manager
    if not manager or not getattr(manager, "mysql_adapter", None):
        raise HTTPException(status_code=503, detail="Database not available")
    return LedgerRepository(manager.mysql_adapter, None)


@router.put("/exchange-daily/{day}")
async def put_exchange_daily(day: date, body: DailyLedger):
    if body.day != day:
        raise HTTPException(
            status_code=422, detail="path day does not match payload day"
        )
    try:
        return await asyncio.to_thread(
            _repo().insert_day, body.model_dump(mode="json"), datetime.now(UTC)
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Ledger database unavailable"
        ) from exc


@router.put("/exchange-positions/{as_of_ms}")
async def put_exchange_positions(as_of_ms: int, body: PositionsSnapshot):
    if body.as_of_ms != as_of_ms:
        raise HTTPException(
            status_code=422, detail="path as_of_ms does not match payload"
        )
    try:
        return await asyncio.to_thread(
            _repo().put_positions, body.model_dump(mode="json"), datetime.now(UTC)
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Ledger database unavailable"
        ) from exc


@router.get("/tieout")
async def get_tieout(
    from_: date = Query(..., alias="from"),
    to: date = Query(...),
):
    if from_ > to or (to - from_).days > 366:
        raise HTTPException(status_code=422, detail="invalid tie-out range")
    try:
        result = await asyncio.to_thread(_repo().tieout, from_, to)
        variances = [Decimal(day["unexplained"]) for day in result["days"]]
        TIEOUT_VARIANCE.labels(window="latest").set(
            float(variances[-1]) if variances else 0
        )
        TIEOUT_VARIANCE.labels(window="worst_in_window").set(
            float(max(variances, key=lambda value: abs(value), default=Decimal("0")))
        )
        TIEOUT_CUMULATIVE.set(float(result["cumulative_variance"]))
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Ledger database unavailable"
        ) from exc


@router.get("/positions-tieout")
async def get_positions_tieout():
    try:
        result = await asyncio.to_thread(_repo().positions_tieout)
        OPEN_POSITION_VARIANCE.set(len(result["phantom_rows"]))
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Ledger database unavailable"
        ) from exc
