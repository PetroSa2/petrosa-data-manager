"""NATS subscriber that persists drawdown breaches into ``drawdown_breaches`` (P4.6-AC6 / #194).

Subscribes to ``alerts.drawdown.breach.>`` (the subject family emitted by
:class:`tradeengine.risk.drawdown_enforcer.DrawdownBreachEmitter`) and
writes each event into the ``drawdown_breaches`` Mongo collection.

AC6.d — tolerant of missing fields: legacy events that predate AC6.a
(no ``envelope_version`` / ``envelope_source``) are persisted with
NULLs in those slots, never dropped.

AC6.b — additive schema: the new nullable fields land alongside the
legacy core fields; no migration of pre-existing rows.

This subscriber runs side-by-side with :class:`AlertDispatcher` (which
also subscribes to ``alerts.>`` and writes to a different collection —
the alert spine). Both subscriptions receive each breach independently
via NATS fan-out; no coordination needed.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import Any

try:
    from datetime import UTC
except ImportError:  # pragma: no cover — py310 compat
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

from pydantic import ValidationError

from data_manager.consumer.nats_client import NATSClient
from data_manager.db.repositories.drawdown_breach_repository import (
    DrawdownBreachRepository,
)
from data_manager.models.drawdown_breach import DrawdownBreach

logger = logging.getLogger(__name__)

DEFAULT_SUBJECT = "alerts.drawdown.breach.>"


def _synthesize_breach_id(payload: dict[str, Any]) -> str:
    """Build a stable breach_id when the producer didn't supply one.

    The pair ``(strategy_id, detected_at)`` is naturally unique for a
    given producer instance; hashing it gives idempotency on replay
    without requiring a UUID dependency.
    """
    strategy_id = str(payload.get("strategy_id", ""))
    detected_at = str(payload.get("detected_at", ""))
    raw = f"{strategy_id}::{detected_at}".encode()
    return hashlib.sha256(raw).hexdigest()[:32]


def _normalize_event(payload: dict[str, Any]) -> dict[str, Any]:
    """Coerce the on-wire payload to the DrawdownBreach model shape.

    Tolerant of missing optional fields per AC6.d — only the legacy
    core fields are required. Type coercions:

    * ``detected_at`` accepts ISO-8601 strings + datetimes
    * ``envelope_version`` accepts int OR str-that-parses-as-int
      (mirrors the producer-side coercion in tradeengine #422)
    """
    out = dict(payload)

    if "breach_id" not in out or not out["breach_id"]:
        out["breach_id"] = _synthesize_breach_id(out)

    if isinstance(out.get("detected_at"), str):
        try:
            out["detected_at"] = datetime.fromisoformat(
                out["detected_at"].replace("Z", "+00:00")
            )
        except ValueError:
            raise ValueError(
                f"detected_at must be ISO-8601; got {out['detected_at']!r}"
            )

    raw_version = out.get("envelope_version")
    if raw_version is not None and not isinstance(raw_version, int):
        try:
            out["envelope_version"] = int(raw_version)
        except (TypeError, ValueError):
            out["envelope_version"] = None  # AC6.d — be tolerant

    return out


class DrawdownBreachSubscriber:
    """Subscribe to drawdown breach events and persist them."""

    def __init__(
        self,
        nats_client: NATSClient | None = None,
        repository: DrawdownBreachRepository | None = None,
        subject: str = DEFAULT_SUBJECT,
    ) -> None:
        self._nats_client = nats_client or NATSClient()
        self._repo = repository
        self._subject = subject
        self._owns_nats = nats_client is None
        self.subscription: Any = None
        self.running = False

    async def start(self) -> bool:
        """Connect (if owned) + subscribe. Returns False on any failure."""
        try:
            if self._owns_nats and not await self._nats_client.connect():
                logger.error(
                    "drawdown_breach_subscriber.start_failed: NATS connect failed"
                )
                return False
            if self._repo is not None:
                try:
                    await self._repo.ensure_indexes()
                except Exception as exc:
                    logger.warning(
                        "drawdown_breach_subscriber.ensure_indexes_failed: %s",
                        exc,
                    )
            self.subscription = await self._nats_client.subscribe(
                subject=self._subject,
                callback=self._on_message,
            )
            self.running = True
            logger.info(
                "drawdown_breach_subscriber.started subject=%s",
                self._subject,
            )
            return True
        except Exception as exc:
            logger.error(
                "drawdown_breach_subscriber.start_error: %s", exc, exc_info=True
            )
            return False

    async def stop(self) -> None:
        self.running = False
        if self.subscription is not None:
            try:
                if hasattr(self.subscription, "unsubscribe"):
                    await self.subscription.unsubscribe()
            except Exception as exc:
                logger.warning("drawdown_breach_subscriber.unsubscribe_error: %s", exc)
            self.subscription = None
        if self._owns_nats:
            try:
                await self._nats_client.close()
            except Exception:
                pass

    async def _on_message(self, msg: Any) -> None:
        """NATS message callback — parse + persist. Never raises."""
        try:
            data = msg.data if hasattr(msg, "data") else msg
            payload = json.loads(data.decode() if isinstance(data, bytes) else data)
        except Exception as exc:
            logger.error(
                "drawdown_breach_subscriber.parse_failed: %s", exc, exc_info=True
            )
            return

        try:
            normalized = _normalize_event(payload)
            breach = DrawdownBreach.model_validate(normalized)
        except (ValueError, ValidationError) as exc:
            logger.error(
                "drawdown_breach_subscriber.validation_failed",
                extra={"error": str(exc), "payload": payload},
            )
            return

        if self._repo is None:
            logger.warning(
                "drawdown_breach_subscriber.no_repo: dropping breach %s",
                breach.breach_id,
            )
            return

        try:
            inserted = await self._repo.insert(breach)
            if inserted:
                logger.info(
                    "drawdown_breach.persisted",
                    extra={
                        "breach_id": breach.breach_id,
                        "strategy_id": breach.strategy_id,
                        "envelope_version": breach.envelope_version,
                    },
                )
        except Exception as exc:
            logger.error(
                "drawdown_breach_subscriber.persist_failed",
                extra={"breach_id": breach.breach_id, "error": str(exc)},
                exc_info=True,
            )
