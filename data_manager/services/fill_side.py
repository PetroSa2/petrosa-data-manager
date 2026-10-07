"""Read-time mapping of legacy exit fills whose ``side`` is the position side (petrosa-data-manager#550).

Until petrosa-tradeengine#743, the exit fills of tradeengine (OCO stop-loss and take-profit, CIO ``exit_now`` and
``scale_out``) were published with the strategy position's side in ``side``: ``LONG`` or ``SHORT``, not the
order side. A consumer that needs ``buy`` / ``sell`` dropped every one of them, so exits vanished from the P&L
replay and from the rounds while the entries counted.

Such a row is mapped, for reading only, to the order side that closes the position (``LONG`` -> ``sell``,
``SHORT`` -> ``buy``) with the position side kept. Only exit-shaped events are mapped: reduce-only, an OCO exit /
``exit_now`` / ``scale_out`` reason, or an event carrying a closure (``close_reason`` or ``position_status``).
Anything else with an unusable side stays unusable. Stored events are never rewritten.
"""

from __future__ import annotations

from typing import Any

LEGACY_EXIT_REASON_PREFIXES = ("oco_exit_", "cio_exit_now", "cio_scale_out")
_CLOSING_SIDE = {"LONG": "sell", "SHORT": "buy"}


def _field(row: dict[str, Any], name: str) -> Any:
    """A field stored on the event or inside its ``payload``."""
    if row.get(name) is not None:
        return row[name]
    payload = row.get("payload")
    return payload.get(name) if isinstance(payload, dict) else None


def is_exit_shaped(row: dict[str, Any]) -> bool:
    """Reduce-only, an exit reason, or an event carrying a closure."""
    if _field(row, "reduce_only") is True:
        return True
    reason = str(row.get("reason") or "").lower()
    if reason.startswith(LEGACY_EXIT_REASON_PREFIXES):
        return True
    return any(
        _field(row, key) is not None for key in ("close_reason", "position_status")
    )


def legacy_exit_side(row: dict[str, Any]) -> tuple[str, str] | None:
    """``(order side, position side)`` of a legacy exit row (side LONG/SHORT on an exit-shaped event)."""
    position_side = str(row.get("side") or "").strip().upper()
    if position_side not in _CLOSING_SIDE or not is_exit_shaped(row):
        return None
    return _CLOSING_SIDE[position_side], position_side


def order_side(row: dict[str, Any]) -> tuple[str, bool]:
    """The row's order side (lower case) and whether it was mapped from a legacy exit."""
    mapped = legacy_exit_side(row)
    if mapped is not None:
        return mapped[0], True
    return str(row.get("side") or "").strip().lower(), False
