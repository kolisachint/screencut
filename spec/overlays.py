"""Overlay intents — template, anchor, text (architecture.md §6.3).

The model chooses from a fixed template set. It does not invent layouts: free-form
generation would be unpredictable, untestable and unlearnable, and under full
autonomy (#12) every instance would be discovered at review time.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated

from pydantic import Field, model_validator

from spec.origin import Stage, spec_field
from spec.types import TIME_EPS, Point, Seconds, SpecModel


class OverlayTemplate(str, Enum):
    """The closed set. Adding one is a template plus a `spec_version` bump."""

    CALLOUT_ARROW = "callout_arrow"
    HIGHLIGHT_BOX = "highlight_box"
    LABEL_CHIP = "label_chip"
    PROGRESS_PILL = "progress_pill"


class OverlayIntent(SpecModel):
    """One overlay, anchored in source time and normalized source space.

    `t_in`/`t_out` are both null for an overlay that spans the whole output — the
    progress pill is the case. That is not a second time base (§4.5): an element
    spanning the whole output needs no anchor at all, so compile runs it from zero
    to the end of whatever it produced. Anything positioned at a *moment* is
    positioned relative to content, which is a source-time anchor like any other,
    and compile drops it if the moment lands inside a removal.
    """

    template: OverlayTemplate = spec_field(produced_by=Stage.PLAN_OVERLAYS)
    text: str = spec_field(default="", produced_by=Stage.PLAN_OVERLAYS)
    anchor: Point | None = spec_field(
        default=None,
        produced_by=Stage.PLAN_OVERLAYS,
        description="Normalized source position. Null for a whole-output overlay, which has nothing to point at.",
    )
    t_in: Seconds | None = spec_field(default=None, produced_by=Stage.PLAN_OVERLAYS)
    t_out: Seconds | None = spec_field(default=None, produced_by=Stage.PLAN_OVERLAYS)

    @model_validator(mode="after")
    def _span(self) -> "OverlayIntent":
        anchored = self.t_in is not None or self.t_out is not None
        if anchored:
            if self.t_in is None or self.t_out is None:
                raise ValueError("an overlay anchored in time needs both t_in and t_out")
            if self.t_out <= self.t_in + TIME_EPS:
                raise ValueError(f"overlay is inverted or empty: [{self.t_in}, {self.t_out}]")
            if self.anchor is None and self.template is not OverlayTemplate.PROGRESS_PILL:
                raise ValueError(f"{self.template.value} is placed at a point and needs an anchor")
        return self

    @property
    def spans_whole_output(self) -> bool:
        return self.t_in is None


class OverlayPlan(SpecModel):
    """The fragment `plan_overlays` returns (§7.2). Small and heavily typed on
    purpose: normalized coordinates and enum template names mean most wrong
    answers are *invalid* answers, which is risk R5's real mitigation."""

    overlays: list[OverlayIntent] = spec_field(default_factory=list, produced_by=Stage.PLAN_OVERLAYS)
