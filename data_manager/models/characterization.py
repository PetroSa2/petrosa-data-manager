"""Characterization artifact model (P3.2, petrosa_k8s#599).

A characterization is a structured, byte-reproducible snapshot of a
strategy's expected behavior over a frozen data window: edge metrics,
drawdown envelope, parameter sensitivities, and the exact inputs that
produced them. The reproducibility property is the point — running the
backtest again against the same `inputs_hash` MUST yield the same
metrics, byte for byte.

The persistence schema is intentionally additive: new metric fields may
be appended to `metrics` without breaking older readers, and
`param_sensitivities` is an open dict so each strategy can describe its
own grid shape.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

try:
    from datetime import UTC
except ImportError:  # pragma: no cover — py310 compatibility
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

from pydantic import BaseModel, Field

REQUIRED_METRIC_KEYS = ("sharpe", "win_rate", "mean_return")


class StrategyRevisionRef(BaseModel):
    """FR53 / P3.4 — typed reference to a strategy revision.

    Mirrors the producer's nested shape (petrosa-bot-ta-analysis
    `backtest/strategy_revision.py`, PR #250). The flat
    ``strategy_revision_id`` carries the short, sortable display form
    (``srev_{module_hash[:12]}_{parameter_hash[:12]}``) and is the field
    consumers filter / refuse on; the nested object preserves the full
    SHA-256 hashes so any byte-level audit can recompute hashes without
    a producer round-trip.
    """

    revision_id: str = Field(
        ...,
        description=(
            "Short revision identifier — "
            "`srev_{module_hash[:12]}_{parameter_hash[:12]}`."
        ),
    )
    module_hash: str = Field(
        ..., description="Full 64-hex SHA-256 of the strategy module source"
    )
    parameter_hash: str = Field(
        ..., description="Full 64-hex SHA-256 of the canonicalized parameter set"
    )


class Characterization(BaseModel):
    """A single backtest characterization persisted in `characterizations`."""

    strategy_id: str = Field(..., description="Strategy identifier")
    strategy_version: str = Field(..., description="Strategy version tag")
    data_window_from: datetime = Field(..., description="Backtest window start (UTC)")
    data_window_to: datetime = Field(..., description="Backtest window end (UTC)")
    seed: int = Field(..., description="Deterministic RNG seed used for the run")
    metrics: dict[str, float] = Field(
        ...,
        description=(
            "Expected edge metrics. Must include sharpe, win_rate, mean_return; "
            "extra keys are accepted and round-tripped"
        ),
    )
    drawdown_envelope: list[float] = Field(
        ...,
        description=(
            "Percentile distribution of drawdowns over the run, expressed as a "
            "list of percentile values (e.g., [p50, p90, p99, p100])"
        ),
    )
    param_sensitivities: dict[str, Any] = Field(
        default_factory=dict,
        description="Small grid of parameter sensitivities around production params",
    )
    inputs_hash: str = Field(
        ...,
        description=(
            "Hex SHA-256 of the deterministic inputs (strategy_id, version, "
            "window bounds, seed, params). Reproducibility key."
        ),
    )
    # FR53 / P3.4 (#179): content-addressable strategy revision binding.
    # `strategy_revision_id` is the flat lookup / filter key; `strategy_revision`
    # carries the nested full-hash provenance. Both default `None` so existing
    # documents (persisted before the producer side shipped) round-trip cleanly.
    strategy_revision_id: str | None = Field(
        default=None,
        description=(
            "Content-addressable strategy-revision identifier "
            "`srev_{module_hash[:12]}_{parameter_hash[:12]}`. "
            "None on artifacts persisted before P3.4 producer shipped."
        ),
    )
    strategy_revision: StrategyRevisionRef | None = Field(
        default=None,
        description=(
            "Full provenance (module_hash + parameter_hash) for the revision; "
            "callers needing strict byte-level reproducibility audit hash from here."
        ),
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When this characterization was persisted",
    )

    def validate_required_metrics(self) -> None:
        """Raise ValueError if metrics is missing any required key.

        Pydantic accepts arbitrary dicts; the schema-level constraint
        that `sharpe`, `win_rate`, and `mean_return` must be present is
        enforced separately so external callers can reuse the helper.
        """
        missing = [k for k in REQUIRED_METRIC_KEYS if k not in self.metrics]
        if missing:
            raise ValueError(
                f"metrics is missing required key(s): {', '.join(missing)}"
            )


def compute_inputs_hash(
    *,
    strategy_id: str,
    strategy_version: str,
    data_window_from: datetime,
    data_window_to: datetime,
    seed: int,
    params: dict[str, Any] | None = None,
) -> str:
    """Compute the canonical SHA-256 hash of a characterization's inputs.

    Inputs are normalised to a deterministic JSON document so two callers
    that build the same characterization produce the same hash regardless
    of dict ordering or timezone naive-vs-aware datetimes.
    """

    def _iso(dt: datetime) -> str:
        # Naive datetimes are assumed UTC so the hash stays stable when a
        # caller forgets to tag tzinfo.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat()

    payload = {
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "data_window_from": _iso(data_window_from),
        "data_window_to": _iso(data_window_to),
        "seed": int(seed),
        "params": params or {},
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def verify_characterization(
    artifact: Characterization,
    recompute_fn,
) -> bool:
    """Re-run the backtest and assert the recomputed characterization matches.

    `recompute_fn` is expected to be a callable that takes the
    deterministic inputs and returns a new ``Characterization`` instance.
    The two artifacts compare equal iff every byte of the canonical
    metric blob matches.

    The check is intentionally strict — partial matches that "drift"
    over time would defeat the reproducibility property. Callers that
    want a softer check should wrap this helper with their own tolerance.
    """
    recomputed = recompute_fn(
        strategy_id=artifact.strategy_id,
        strategy_version=artifact.strategy_version,
        data_window_from=artifact.data_window_from,
        data_window_to=artifact.data_window_to,
        seed=artifact.seed,
    )

    def _blob(c: Characterization) -> bytes:
        # `strategy_revision_id` participates in the byte-equality check so a
        # re-characterization against the same metrics but a different strategy
        # revision (FR53 / P3.4) is correctly detected as drift, not a match.
        return json.dumps(
            {
                "metrics": c.metrics,
                "drawdown_envelope": c.drawdown_envelope,
                "param_sensitivities": c.param_sensitivities,
                "inputs_hash": c.inputs_hash,
                "strategy_revision_id": c.strategy_revision_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    return _blob(artifact) == _blob(recomputed)
