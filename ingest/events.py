"""Recorder events -> `FocusTrack` (architecture.md §4.3, risk R1).

`RecorderEvents` is deliberately the poorest plausible recorder export: sampled
cursor positions in pixels, plus click timestamps. Nothing here assumes window
bounds, keystrokes or scroll deltas even if a recorder turns out to expose them.

`extra="ignore"`, because a real recorder will emit fields we do not care about
and an adapter that refuses to parse them is a brittle adapter. That is the
opposite of the spec's `extra="forbid"`, and deliberately so: this is somebody
else's format, and that one is ours.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from spec.focus import FocusKind, FocusPoint, FocusTrack


class CursorSample(BaseModel):
    model_config = ConfigDict(extra="ignore")

    t: float = Field(ge=0.0, description="Seconds from recording start.")
    x: float = Field(description="Pixels from the left of the recorded frame.")
    y: float = Field(description="Pixels from the top of the recorded frame.")


class RecorderEvents(BaseModel):
    """What we ask a recorder for, and no more."""

    model_config = ConfigDict(extra="ignore")

    width: int = Field(gt=0)
    height: int = Field(gt=0)
    duration: float = Field(gt=0.0)
    samples: list[CursorSample] = Field(default_factory=list)
    clicks: list[float] = Field(default_factory=list, description="Click timestamps in seconds.")

    @classmethod
    def load(cls, path: str | Path) -> "RecorderEvents":
        return cls.model_validate_json(Path(path).read_text())


#: How much each kind of attention pulls the frame. Clicks are intent; movement
#: is mostly the cursor being somewhere. These are the adapter's only judgement
#: calls, and they are scalars the preference store can learn later (§10).
CLICK_WEIGHT = 1.0
DWELL_WEIGHT = 0.6
MOVEMENT_WEIGHT = 0.3

DEFAULT_CLICK_WINDOW_S = 0.05
"""Fallback for how close a sample must be to a click timestamp to *be* that click.
Used only when the sample rate cannot be measured; otherwise one sample interval,
so a click marks the sample it happened on rather than a handful either side."""

DEFAULT_DWELL_RADIUS = 0.01
"""Normalized distance below which consecutive samples count as the cursor resting."""


def to_focus_track(
    events: RecorderEvents,
    *,
    click_window_s: float | None = None,
    dwell_radius: float = DEFAULT_DWELL_RADIUS,
) -> FocusTrack:
    """Normalize, classify, and hand back our format.

    Deterministic and cheap: this is the §4.3 argument that no model participates
    in the highest-impact spatial decision in the pipeline.
    """
    samples = sorted(events.samples, key=lambda s: s.t)
    clicks = sorted(events.clicks)
    if click_window_s is None:
        click_window_s = _sample_interval(samples)
    points: list[FocusPoint] = []
    for index, sample in enumerate(samples):
        x = min(max(sample.x / events.width, 0.0), 1.0)
        y = min(max(sample.y / events.height, 0.0), 1.0)
        kind = FocusKind.MOVEMENT
        weight = MOVEMENT_WEIGHT
        if any(abs(sample.t - c) <= click_window_s for c in clicks):
            kind, weight = FocusKind.CLICK, CLICK_WEIGHT
        elif index and _near(samples[index - 1], sample, events, dwell_radius):
            kind, weight = FocusKind.DWELL, DWELL_WEIGHT
        points.append(FocusPoint(t=sample.t, x=x, y=y, weight=weight, kind=kind))
    return FocusTrack(points=points)


def _sample_interval(samples: list[CursorSample]) -> float:
    """Median gap between samples — the width of "the sample this click landed on"."""
    if len(samples) < 2:
        return DEFAULT_CLICK_WINDOW_S
    gaps = sorted(b.t - a.t for a, b in zip(samples, samples[1:]))
    return gaps[len(gaps) // 2] or DEFAULT_CLICK_WINDOW_S


def _near(a: CursorSample, b: CursorSample, events: RecorderEvents, radius: float) -> bool:
    dx = (a.x - b.x) / events.width
    dy = (a.y - b.y) / events.height
    return (dx * dx + dy * dy) ** 0.5 <= radius


def write_events(events: RecorderEvents, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(events.model_dump(), indent=2) + "\n")
    return path
