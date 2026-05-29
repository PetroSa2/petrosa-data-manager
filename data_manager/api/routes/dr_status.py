"""DR status dashboard endpoint (petrosa_k8s#743, P9-AC5.c, NFR-R6).

Single endpoint that surfaces the latest audit-trail restore-exercise
verdict to the operator dashboard. The petrosa_k8s side (#743) ships
the 90-day CronJob that writes ``restore_exercises`` rows; this route
reads the latest row and returns the canonical AC5.c triple:

  * ``last_exercise_at``
  * ``last_exercise_outcome``
  * ``days_since_last_exercise``

Plus the snapshot id and operator identity from the row so the
dashboard can render a one-glance DR-health card without a second
round-trip.

Mounted at ``/api/dashboard/dr-status`` from
:func:`data_manager.api.app.create_app`. Patterns track the
leverage-bounds routes shipped in #182 (PR #186): in-path
``/api/dashboard`` prefix, error-as-RFC7807 problem JSON, MongoDB
adapter pulled off ``api_module.db_manager``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

try:
    from datetime import UTC
except ImportError:  # pragma: no cover — py310 compatibility
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

from fastapi import APIRouter, HTTPException

import data_manager.api.app as api_module

logger = logging.getLogger(__name__)

router = APIRouter()


RESTORE_EXERCISES_COLLECTION = "restore_exercises"


def _require_mongo():  # type: ignore[no-untyped-def]
    if not api_module.db_manager or not getattr(
        api_module.db_manager, "mongodb_adapter", None
    ):
        raise HTTPException(
            status_code=503,
            detail={
                "title": "MongoDB unavailable",
                "detail": "data-manager is not connected to MongoDB",
            },
        )
    return api_module.db_manager.mongodb_adapter


def _coerce_exercised_at(value: Any) -> datetime | None:
    """Mongo rows store ``exercised_at`` as a BSON Date, which motor
    decodes as a ``datetime``. Defensive fallback for string-coerced rows
    written by ad-hoc scripts."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


@router.get("/api/dashboard/dr-status")
async def dr_status() -> dict[str, Any]:
    """Latest restore-exercise verdict + age in days.

    Response shape (AC5.c):

    ::

        {
            "last_exercise_at":     "<ISO-8601 UTC>" | null,
            "last_exercise_outcome": "pass" | "fail" | null,
            "days_since_last_exercise": <int> | null,
            "snapshot_id":           "<string>" | null,
            "operator":              "<string>" | null
        }

    All fields are ``null`` when the collection has no rows
    (no exercise ever ran). The dashboard renders this as
    "DR exercise never run — schedule one" with a red status badge
    via NFR-R6 AC5's cadence rule.
    """
    mongo = _require_mongo()
    col = mongo.db[RESTORE_EXERCISES_COLLECTION]
    doc = await col.find_one({}, sort=[("exercised_at", -1)])

    if doc is None:
        return {
            "last_exercise_at": None,
            "last_exercise_outcome": None,
            "days_since_last_exercise": None,
            "snapshot_id": None,
            "operator": None,
        }

    exercised_at = _coerce_exercised_at(doc.get("exercised_at"))
    if exercised_at is None:
        days_since = None
        iso_exercised_at = None
    else:
        now = datetime.now(UTC)
        days_since = int((now - exercised_at).total_seconds() // 86400)
        iso_exercised_at = exercised_at.astimezone(UTC).isoformat().replace(
            "+00:00", "Z"
        )

    return {
        "last_exercise_at": iso_exercised_at,
        "last_exercise_outcome": doc.get("outcome"),
        "days_since_last_exercise": days_since,
        "snapshot_id": doc.get("snapshot_id"),
        "operator": doc.get("operator"),
    }
