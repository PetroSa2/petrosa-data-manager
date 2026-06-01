"""Pydantic model for the ``drawdown_breaches`` Mongo collection (P4.6-AC6 / #194).

Mirrors the on-wire shape of the NATS event published by
:class:`tradeengine.risk.drawdown_enforcer.DrawdownBreachEmitter` on
``alerts.drawdown.breach.{strategy_id}`` — see
``petrosa-tradeengine/tradeengine/risk/drawdown_enforcer.py``.

AC6.b nullable fields ``envelope_version`` + ``envelope_source`` were
added by AC6.a (#422/#441) on the producer side; legacy breaches recorded
before that ship stay with NULLs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

EnvelopeSource = Literal["operator_approved", "characterization"]


class DrawdownBreach(BaseModel):
    """One row in the ``drawdown_breaches`` collection.

    The Mongo ``_id`` is ``breach_id`` (we synthesise one from
    ``strategy_id + detected_at`` if the producer didn't supply one so the
    insert path is naturally idempotent).
    """

    breach_id: str = Field(
        ...,
        min_length=1,
        description="Stable identifier — becomes the Mongo _id.",
    )
    strategy_id: str = Field(
        ...,
        min_length=1,
        description="Strategy whose drawdown breached its envelope value.",
    )
    observed_drawdown_pct: float = Field(
        ...,
        description="Measured drawdown at breach time, expressed as positive percent.",
    )
    envelope_value_pct: float = Field(
        ...,
        description="The envelope value the drawdown was compared against.",
    )
    exceeded_by_pct: float = Field(
        ...,
        description="``observed_drawdown_pct - envelope_value_pct``.",
    )
    detected_at: datetime = Field(
        ...,
        description="UTC timestamp when the producer detected the breach.",
    )
    # AC6.b — additive, nullable (legacy rows stay null).
    envelope_version: int | None = Field(
        default=None,
        description=(
            "Version of the envelope the drawdown was measured against. "
            "NULL for breaches predating AC6.a (#422) — see /api/breaches/{id} "
            "AC6.c, which returns envelope=null in that case."
        ),
    )
    envelope_source: EnvelopeSource | None = Field(
        default=None,
        description=(
            "Provenance of the envelope (mirrors Envelope.source). "
            "NULL when the breach predates AC6.a or the fetcher fell back "
            "to the legacy stub at producer time."
        ),
    )
