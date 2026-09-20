"""NATS breach publisher for portfolio drawdown (P4.2, #602).

Publishes envelope-breach events on
``alerts.portfolio.drawdown.breach.{strategy_id}`` so CIO's
``alerts.>`` consumer picks them up and forwards them to Telegram
per FR30. The payload embeds the verbatim ``DrawdownResult``
``to_dict()`` shape (so it still matches the HTTP endpoint's payload)
alongside the ``category``/``severity``/``message``/``timestamp``
envelope CIO's alerts consumer expects (see
``cio/core/alerts_consumer.py`` and ``cio/core/alerting/fr66_alerts.py``
in petrosa-cio).

petrosa-cio#215: this used to publish on ``portfolio.drawdown.breach.*``,
a subject nobody subscribed to (CIO's ``alerts.>`` consumer never
matches a subject that doesn't start with ``alerts.``). It is
deliberately NOT renamed to ``alerts.drawdown.breach.*`` — that subject
family is already owned by tradeengine's PER-POSITION drawdown breach
(``tradeengine/risk/drawdown_enforcer.py``) and consumed by
:class:`data_manager.services.drawdown_breach_subscriber.DrawdownBreachSubscriber`,
which persists into the ``drawdown_breaches`` Mongo collection using a
schema (``observed_drawdown_pct`` / ``envelope_value_pct`` /
``exceeded_by_pct`` / ``detected_at``) that does not match this
PORTFOLIO-level payload (``current_drawdown_pct`` /
``envelope_threshold_pct`` / ``breach_percentile``). Publishing under
that prefix would make every portfolio breach fail that subscriber's
Pydantic validation and log a spurious warning. ``alerts.portfolio.*``
keeps this event distinct from the position-level family while still
reaching a real, already-running consumer.

The publisher is intentionally narrow: ``maybe_publish`` only fires
when ``result.breached`` is True. Healthy ticks are NOT published —
that would flood NATS at the scheduler's cadence × number of
strategies and provide no signal beyond "still healthy".
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from data_manager.consumer.nats_client import NATSClient
    from data_manager.portfolio.drawdown_service import DrawdownResult

logger = logging.getLogger(__name__)


BREACH_SUBJECT_PREFIX = "alerts.portfolio.drawdown.breach"

CATEGORY_PORTFOLIO_DRAWDOWN_BREACH = "portfolio_drawdown_breach"
SEVERITY_CRITICAL = "critical"


def _build_alert_payload(result: DrawdownResult) -> dict[str, Any]:
    """Wrap ``DrawdownResult.to_dict()`` in the FR66-style alert envelope.

    Keeps every original key (dashboards/tests rely on the verbatim
    ``to_dict()`` shape) and adds the fields CIO's generic ``alerts.>``
    Telegram consumer reads (``category``, ``severity``, ``message``,
    ``timestamp``) so the alert renders with real content instead of
    "(no message)".
    """
    base = result.to_dict()
    message = (
        f"Portfolio drawdown breach on strategy_id={result.strategy_id}: "
        f"current={result.current_drawdown_pct:.2f}% > "
        f"threshold={result.envelope_threshold_pct}% "
        f"(percentile={result.breach_percentile})"
    )
    return {
        **base,
        "category": CATEGORY_PORTFOLIO_DRAWDOWN_BREACH,
        "severity": SEVERITY_CRITICAL,
        "message": message,
        "timestamp": datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
    }


class DrawdownBreachPublisher:
    """Publishes envelope-breach events on ``alerts.portfolio.drawdown.breach.>``."""

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
        payload = _build_alert_payload(result)
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
