"""Typed durable signal records and replay responses."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class SignalRecord(BaseModel):
    """Canonical durable signal payload.

    New fields are optional so legacy ingestion remains compatible; replay filters
    those rows unless explicitly requested.
    """

    model_config = ConfigDict(extra="allow")

    symbol: str
    timeframe: str = "15m"
    period: str | None = None
    signal_type: str = "hold"
    action: str | None = None
    confidence: float = 0.0
    strategy: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime | None = None
    signal_key: str | None = None
    bar_open_time: datetime | None = None
    bar_close_time: datetime | None = None
    entry_ref_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    decision_id: str | None = None

    @field_validator("timestamp", "bar_open_time", "bar_close_time")
    @classmethod
    def normalize_times(cls, value: datetime | None) -> datetime | None:
        return _utc_datetime(value)

    def model_dump_for_storage(self) -> dict[str, Any]:
        values = self.model_dump(exclude_none=True)
        if self.timestamp is None:
            values["timestamp"] = datetime.now(UTC)
        if self.period is None:
            values["period"] = self.timeframe
        if self.action:
            values["signal_type"] = self.action
        values.pop("action", None)
        return values


class SignalReplayRow(BaseModel):
    """Public replay row, including optional execution outcome."""

    model_config = ConfigDict(extra="allow")

    signal_key: str | None = None
    symbol: str
    timeframe: str | None = None
    strategy: str | None = None
    bar_open_time: datetime | None = None
    bar_close_time: datetime | None = None
    entry_ref_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    decision_id: str | None = None
    execution_id: str | None = None
    fill_outcome: dict[str, Any] | None = None


class SignalCoverage(BaseModel):
    signal_count: int
    klines_present: int
    missing_kline_bars: list[datetime]
    kline_source: str
    last_kline_time: datetime | None
    legacy_rows: int
