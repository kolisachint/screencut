"""Recorder events -> `FocusTrack` (architecture.md §4.3, risk R1).

`RecorderEvents` is deliberately the poorest plausible recorder export: sampled
cursor positions in pixels, plus click timestamps. Nothing here assumes window
bounds, keystrokes or scroll deltas even if a recorder turns out to expose them.

`extra="ignore"`, because a real recorder will emit fields we do not care about
and an adapter that refuses to parse them is a brittle adapter. That is the
opposite of the spec's `extra="forbid"`, and deliberately so: this is somebody
else's format, and that one is ours.

`RecorderEvents` is one adapter's input; `classify` is every adapter's output
path. Cap (`ingest/cap.py`) skips the model entirely and calls `classify` with
`Sample`s it built itself, because its coordinates are normalized at source and
routing them through a pixel model and back would round twice for nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
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
"""Normalized distance the cursor may wander and still count as resting."""

DEFAULT_DWELL_WINDOW_S = 0.3
"""How far back to look when deciding the cursor is resting.

Per-sample displacement is the wrong test: a cursor easing across the screen moves
less between two adjacent samples than a resting hand does over a second, so a
sample-to-sample radius classifies slow travel as dwell — and every zoom region in
the video then merges into one."""


@dataclass(frozen=True)
class Sample:
    """A cursor position in normalized source coordinates, at a source time.

    The common currency of every adapter. `RecorderEvents` arrives in pixels and
    is divided down; Cap arrives normalized already (environment findings §2) and
    is passed through. Classification happens once, here, in the one space
    `FocusTrack` is defined in — a second classifier written against pixels would
    be the same dwell rule twice, and the pair that drifts.
    """

    t: float
    x: float
    y: float


def classify(
    samples: list[Sample],
    click_times: list[float],
    *,
    click_window_s: float | None = None,
    dwell_radius: float = DEFAULT_DWELL_RADIUS,
    dwell_window_s: float = DEFAULT_DWELL_WINDOW_S,
) -> FocusTrack:
    """Label each sample movement, click or dwell, and weight it.

    Deterministic and cheap: this is the §4.3 argument that no model participates
    in the highest-impact spatial decision in the pipeline.
    """
    samples = sorted(samples, key=lambda s: s.t)
    clicks = sorted(click_times)
    if click_window_s is None:
        click_window_s = _sample_interval(samples)
    points: list[FocusPoint] = []
    for index, sample in enumerate(samples):
        x = min(max(sample.x, 0.0), 1.0)
        y = min(max(sample.y, 0.0), 1.0)
        kind = FocusKind.MOVEMENT
        weight = MOVEMENT_WEIGHT
        if any(abs(sample.t - c) <= click_window_s for c in clicks):
            kind, weight = FocusKind.CLICK, CLICK_WEIGHT
        elif _resting(samples, index, dwell_radius, dwell_window_s):
            kind, weight = FocusKind.DWELL, DWELL_WEIGHT
        points.append(FocusPoint(t=sample.t, x=x, y=y, weight=weight, kind=kind))
    return FocusTrack(points=points)


def to_focus_track(
    events: RecorderEvents,
    *,
    click_window_s: float | None = None,
    dwell_radius: float = DEFAULT_DWELL_RADIUS,
    dwell_window_s: float = DEFAULT_DWELL_WINDOW_S,
) -> FocusTrack:
    """Normalize, then hand the shared classifier our format."""
    return classify(
        [
            Sample(t=s.t, x=s.x / events.width, y=s.y / events.height)
            for s in events.samples
        ],
        events.clicks,
        click_window_s=click_window_s,
        dwell_radius=dwell_radius,
        dwell_window_s=dwell_window_s,
    )


def _sample_interval(samples: list[Sample]) -> float:
    """Median gap between samples — the width of "the sample this click landed on"."""
    if len(samples) < 2:
        return DEFAULT_CLICK_WINDOW_S
    gaps = sorted(b.t - a.t for a, b in zip(samples, samples[1:]))
    return gaps[len(gaps) // 2] or DEFAULT_CLICK_WINDOW_S


def _resting(
    samples: list[Sample],
    index: int,
    radius: float,
    window_s: float,
) -> bool:
    """True when the cursor stayed within `radius` for the whole preceding window."""
    if index == 0:
        return False
    current = samples[index]
    back = index - 1
    seen = False
    while back >= 0 and current.t - samples[back].t <= window_s:
        if _distance(samples[back], current) > radius:
            return False
        seen = True
        back -= 1
    return seen and current.t - samples[max(back + 1, 0)].t >= window_s * 0.5


def _distance(a: Sample, b: Sample) -> float:
    return ((a.x - b.x) ** 2 + (a.y - b.y) ** 2) ** 0.5


def write_events(events: RecorderEvents, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(events.model_dump(), indent=2) + "\n")
    return path
