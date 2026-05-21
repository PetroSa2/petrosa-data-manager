"""NATS breach publisher for portfolio drawdown (P4.2, #602).

Publishes envelope-breach events on
``portfolio.drawdown.breach.{strategy_id}`` so CIO can subscribe and
intervene per FR30. The payload is the verbatim ``DrawdownResult``
``to_dict()`` so consumers can read the same shape the HTTP endpoint
returns.

The publisher is intentionally narrow: ``maybe_publish`` only fires
when ``result.breached`` is True. Healthy ticks are NOT published —
that would flood NATS at the scheduler's cadence × number of
strategies and provide no signal beyond "still healthy".
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from data_manager.consumer.nats_client import NATSClient
    from data_manager.portfolio.drawdown_service import DrawdownResult

logger = logging.getLogger(__name__)


BREACH_SUBJECT_PREFIX = "portfolio.drawdown.breach"


class DrawdownBreachPublisher:
    """Publishes envelope-breach events on ``portfolio.drawdown.breach.>``."""

    def __init__(self, nats_client: NATSClient) -> None:
        self._nc = nats_client

    async def maybe_publish(self, result: DrawdownResult) -> bool:
        """Publish a breach event iff ``result.breached`` is True.

        Returns True iff a publish was attempted (the underlying client
        may still drop on disconnect; that's logged but not raised so
        the scheduler loop survives transient NATS issues).
        """
        if not result.breached:
            return False
        subject = f"{BREACH_SUBJECT_PREFIX}.{result.strategy_id}"
        payload = result.to_dict()
        try:
            data = json.dumps(payload).encode()
            await self._nc.publish(subject, data)
            logger.info(
                "drawdown_breach_published",
                extra={
                    "subject": subject,
                    "strategy_id": result.strategy_id,
                    "drawdown_pct": result.current_drawdown_pct,
                    "threshold_pct": result.envelope_threshold_pct,
                    "percentile": result.breach_percentile,
                },
            )
            return True
        except Exception as exc:  # noqa: BLE001 — never crash the scheduler
            logger.error(
                "drawdown_breach_publish_failed",
                extra={"subject": subject, "error": str(exc)},
            )
            return False
