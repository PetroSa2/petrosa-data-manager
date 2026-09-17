"""
Shared sanitization helpers for the analytics pipeline.

Analytics calculators frequently derive Decimal values from statistical
operations (correlation of constant series, division by zero windows,
empty samples, etc.) that can legitimately produce NaN or +/-Infinity.
Pydantic v2 rejects non-finite Decimal/float values by default
(``finite_number`` validation error), which previously crashed the whole
calculator for every symbol in the batch instead of degrading gracefully
for just the offending value.

Use :func:`safe_decimal` (or :func:`is_non_finite` for boolean checks)
at every boundary where a computed number is about to be stored on a
Pydantic analytics model, per #315.
"""

from __future__ import annotations

import logging
import math
from decimal import Decimal, InvalidOperation
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["is_non_finite", "safe_decimal"]


def is_non_finite(value: Any) -> bool:
    """
    Return True if `value` is NaN, +/-Infinity, or cannot be interpreted
    as a finite number at all.

    Accepts Decimal, float, int, numpy scalars, or numeric strings.
    None is treated as finite (callers decide how to handle missing data
    separately) so this helper only flags genuinely non-finite numbers.
    """
    if value is None:
        return False

    if isinstance(value, Decimal):
        return value.is_nan() or value.is_infinite()

    try:
        return not math.isfinite(float(value))
    except (TypeError, ValueError):
        # Not numeric at all -- treat as non-finite so callers substitute
        # a safe default rather than letting a downstream Decimal(str(...))
        # raise InvalidOperation.
        return True


def safe_decimal(
    value: Any,
    default: Decimal | None = Decimal("0"),
    *,
    context: str = "",
) -> Decimal | None:
    """
    Convert `value` to a finite Decimal, substituting `default` (with a
    WARNING log naming `context`) when the value is NaN, +/-Infinity, or
    otherwise not a valid number.

    Args:
        value: Raw value to convert (Decimal, float, int, numpy scalar, str).
        default: Value to return when `value` is non-finite/invalid.
            Pass None to allow an Optional[Decimal] field to fall back to
            None instead of a numeric placeholder.
        context: Short human-readable description of what is being
            computed (e.g. "BTCUSDT correlation_matrix.DOTUSDT") — included
            in the warning log so operators can trace which symbol/metric
            was affected.

    Returns:
        A finite Decimal, or `default` if the input was non-finite/invalid.
    """
    if value is None:
        return default

    if isinstance(value, Decimal):
        candidate = value
    else:
        try:
            candidate = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            logger.warning(
                "Non-numeric value for %s: %r; substituting default %s",
                context or "analytics field",
                value,
                default,
            )
            return default

    if candidate.is_nan() or candidate.is_infinite():
        logger.warning(
            "Non-finite value (%s) for %s; substituting default %s",
            candidate,
            context or "analytics field",
            default,
        )
        return default

    return candidate
