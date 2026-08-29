"""FocusTrack — where we look (architecture.md §4.3).

One time series serves all three output types: zoom keyframes for 16:9, a crop
path for 9:16, a Ken Burns pan for a still. Modelling it this way is what makes
the photo path the degenerate case of the video path rather than a parallel
implementation.

No model participates. Everything here is ingest arithmetic; the projection into
a profile is arithmetic too (§4.3), governed by scalars the preference store can
learn by median.
"""

from __future__ import annotations

from enum import Enum

from pydantic import model_validator

from spec.origin import Stage, spec_field
from spec.types import TIME_EPS, Normalized, Seconds, SpecModel


class FocusKind(str, Enum):
    """Why attention is at this point, which decides how much it counts for."""

    MOVEMENT = "movement"
    CLICK = "click"
    DWELL = "dwell"
    MANUAL = "manual"
    """A hand-placed point — the still path, or a review-UI correction."""


class FocusPoint(SpecModel):
    t: Seconds = spec_field(produced_by=Stage.INGEST, description="Source time in seconds.")
    x: Normalized = spec_field(produced_by=Stage.INGEST)
    y: Normalized = spec_field(produced_by=Stage.INGEST)
    weight: Normalized = spec_field(
        default=1.0,
        produced_by=Stage.INGEST,
        description="How strongly this point attracts framing. Clicks outweigh movement.",
    )
    kind: FocusKind = spec_field(default=FocusKind.MOVEMENT, produced_by=Stage.INGEST)


class FocusTrack(SpecModel):
    points: list[FocusPoint] = spec_field(
        default_factory=list,
        produced_by=Stage.INGEST,
        description="Sampled attention, ascending in t. May be empty — a still with no manual point.",
    )

    @model_validator(mode="after")
    def _ascending(self) -> "FocusTrack":
        for prev, nxt in zip(self.points, self.points[1:]):
            if nxt.t < prev.t - TIME_EPS:
                raise ValueError(f"focus points must ascend in t: {prev.t} then {nxt.t}")
        return self

    @property
    def duration_covered(self) -> float:
        return self.points[-1].t - self.points[0].t if self.points else 0.0

    def clicks(self) -> list[FocusPoint]:
        return [p for p in self.points if p.kind is FocusKind.CLICK]

    def between(self, t_in: float, t_out: float) -> list[FocusPoint]:
        return [p for p in self.points if t_in - TIME_EPS <= p.t < t_out - TIME_EPS]
