"""Shared scalar types and the tolerances that go with them.

Two rules from architecture.md §4.1 are enforced here rather than repeated at
every use site:

- every spatial value is normalized to 0.0-1.0 in *source* coordinates,
- every temporal value is seconds from *source* start.

There are no pixels in this package, and there is no second time base (§4.5).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Slack allowed when comparing two times in seconds. Interval arithmetic on
#: floats needs a tolerance; one microsecond is far below any perceivable edit
#: boundary and far above float64 noise over a recording-length timeline.
TIME_EPS = 1e-6

Normalized = Annotated[float, Field(ge=0.0, le=1.0)]
"""A spatial coordinate or fraction in source space."""

Seconds = Annotated[float, Field(ge=0.0)]
"""A time in seconds from source start."""

PositiveSeconds = Annotated[float, Field(gt=0.0)]

Decibels = float
"""A level in decibels. Named for the reader; unconstrained on purpose, since
both gains and negative ducking amounts are ordinary values."""

Tunable = int | float
"""One learnable scalar, addressed by dotted path rather than by field.

Used where a document carries a *correction to* another document's field rather
than the field itself (`spec/corrections.py`). Numeric because §10 learns by
windowed median, and a median is what a learnable tunable has to have — which is
also why fonts and focus modes are excluded at the field (`spec/profiles.py`)
rather than tolerated here. The value's real constraints belong to the field
being corrected, and re-validating that model is what applies them.

`int` first: pydantic's smart union keeps an exact type, but the declaration
order is what a reader checks, and a caption line count coerced to 3.0 would
reach `max_chars_per_line` as a float."""


class SpecModel(BaseModel):
    """Base for every spec model.

    `extra="forbid"` is load-bearing under decision #13: with no constrained
    decoding, a model stage returning a plausible-but-wrong extra key should be
    a validation failure that triggers the §7.2 retry, not a field silently
    dropped on the floor.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Point(SpecModel):
    """A position in normalized source coordinates."""

    x: Normalized
    y: Normalized


class Rect(SpecModel):
    """A rectangle in normalized coordinates, origin top-left."""

    x: Normalized
    y: Normalized
    w: Annotated[float, Field(gt=0.0, le=1.0)]
    h: Annotated[float, Field(gt=0.0, le=1.0)]

    @model_validator(mode="after")
    def _inside_the_frame(self) -> "Rect":
        if self.right > 1.0 + TIME_EPS or self.bottom > 1.0 + TIME_EPS:
            raise ValueError(f"rect runs off the frame: right={self.right}, bottom={self.bottom}")
        return self

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h


class TimeSpan:
    """Interval arithmetic for any model carrying `t_in` and `t_out` in source time.

    A mixin rather than a base model on purpose: `t_in` and `t_out` are declared
    by each concrete model so each can record its own producing stage (§11.1).
    A shared base would hard-code one origin for every user of a range.
    """

    if TYPE_CHECKING:  # declared by the concrete model, never by this mixin
        t_in: float
        t_out: float

    @property
    def duration(self) -> float:
        return self.t_out - self.t_in

    def contains(self, t: float) -> bool:
        return self.t_in - TIME_EPS <= t < self.t_out - TIME_EPS

    def overlaps(self, other: "TimeSpan") -> bool:
        return (self.t_in < other.t_out - TIME_EPS) and (other.t_in < self.t_out - TIME_EPS)


def approx_eq(a: float, b: float, eps: float = TIME_EPS) -> bool:
    return abs(a - b) <= eps
