"""Cap recordings -> `Source` + `FocusTrack` (risk R1, environment findings §2).

Written against a real take, measured in phase 0 rather than assumed. A `.cap`
bundle is a directory:

```
<take>.cap/
  recording-meta.json                     segments[].display.{path,start_time,fps}
  content/segments/segment-0/display.mp4  segments[].cursor
  content/segments/segment-0/cursor.json  {"moves": [...], "clicks": [...]}
```

**Cap's coordinates are already normalized to 0..1**, which is `spec/types.py`'s
`Normalized` — so nothing here converts to pixels and back, and the whole class
of rounding bugs `AGENTS.md` warns about never arises.

Four things phase 0 found that an adapter written from the format alone would get
wrong, each handled once and here:

1. **Sampling is event-driven, not on a clock.** Median 37 Hz, but the longest
   observed gap is 1 981 ms, because a resting cursor emits nothing. A gap is not
   missing data — it is the strongest dwell evidence in the file, and reading it
   as data to interpolate across turns the clearest rest in a take into a slow
   glide. `resample` holds through gaps and interpolates only within them.
2. **Clicks carry no position**, only a time and a `down` flag. The nearest move
   sample was up to 423 ms away in the measured take, which is long enough for a
   moving cursor to be somewhere else entirely, so a click's position is
   interpolated between the samples bracketing it rather than snapped to
   whichever is nearer.
3. **Two clocks.** Cursor `time_ms` is on the recording clock; the video starts
   `display.start_time` later. §4.5 permits one time base — seconds from source
   start — so the offset is subtracted once, at this boundary, and never again.
4. **The sidecar's `fps` is not the stream's.** `recording-meta.json` claimed 25
   against a 59 fps stream. `ffprobe` is the authority for every media fact.
"""

from __future__ import annotations

import json
import shutil
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path

from spec.focus import FocusTrack
from spec.source import Provenance, Source
from verify.probe import probe

from ingest.events import Sample, classify

META_NAME = "recording-meta.json"

SYNTHETIC_MARKER = "screencut-synthetic"
"""A file `ingest/cap_fixture.py` drops in the bundles it writes.

The fixture bundle is in Cap's own on-disk format and is read by this adapter
exactly as a recording is — which is the point of it, and which means nothing
downstream can tell the two apart unless the generated one says so. §10.2 needs
that distinction and cannot recover it later (`spec/source.py`), so it is decided
here, at the only boundary that knows.

A generated bundle declaring itself, rather than a real one declaring itself, is
the direction that fails safe: a marker that goes missing makes a fixture look
real, but nothing a recorder writes can make a real take look generated, and the
marker is written by the generator two functions away rather than by a person."""

RESAMPLE_HZ = 30.0
"""The fixed grid `plan_focus` is handed, in samples per second.

`plan_focus` measures dwell over a window and rate-limits crop movement per
frame; both read a cadence out of the samples they are given, and Cap's cadence
is whatever the hand was doing. 30 Hz is the profiles' frame rate — a grid finer
than the output cannot influence a frame, and one coarser loses a click to
rounding."""

HOLD_AFTER_MS = 150.0
"""Longer than this between two moves and the cursor was resting, not travelling.

Cap emits on movement, so silence means stillness. The threshold sits above the
measured p95 gap of 111 ms — under it, a gap is the sampler breathing and the
positions either side are one motion; over it, interpolating invents a glide that
never happened and hides the dwell that did."""


@dataclass(frozen=True)
class CapTake:
    """One Cap segment, resolved to absolute paths and a single time base."""

    root: Path
    video: Path
    cursor: Path
    start_time: float
    """Seconds the video starts *after* the recording clock's zero."""

    moves: list[Sample]
    """Cursor positions, normalized, already shifted into source time."""

    clicks: list[float]
    """Mouse-down times in source time. Ups are not attention."""


def read_take(recording: Path | str, segment: int = 0) -> CapTake:
    """Parse a `.cap` bundle's cursor track into source time.

    Points before the video starts are dropped rather than clamped to zero: they
    happened, but not on this recording, and a pile of them at t=0 reads to
    `plan_focus` as an emphatic dwell on wherever the pointer was parked.
    """
    root = Path(recording)
    meta = json.loads((root / META_NAME).read_text())
    try:
        entry = meta["segments"][segment]
    except (KeyError, IndexError):
        raise ValueError(f"{root} has no segment {segment}") from None

    display = entry["display"]
    video = root / display["path"]
    cursor_path = root / entry["cursor"]
    start_time = float(display.get("start_time") or 0.0)

    cursor = json.loads(cursor_path.read_text())
    moves = [
        Sample(t=m["time_ms"] / 1000.0 - start_time, x=float(m["x"]), y=float(m["y"]))
        for m in cursor.get("moves", [])
    ]
    moves = sorted((m for m in moves if m.t >= 0.0), key=lambda m: m.t)
    clicks = sorted(
        c["time_ms"] / 1000.0 - start_time
        for c in cursor.get("clicks", [])
        if c.get("down")
    )
    return CapTake(
        root=root,
        video=video,
        cursor=cursor_path,
        start_time=start_time,
        moves=moves,
        clicks=[c for c in clicks if c >= 0.0],
    )


def position_at(moves: list[Sample], t: float) -> tuple[float, float]:
    """Where the cursor was at `t`, interpolated between bracketing samples.

    This is trap 2's fix and trap 1's mechanism at once. Between two samples less
    than `HOLD_AFTER_MS` apart the cursor was in motion, so interpolate; across a
    longer gap it was not moving at all, so hold the earlier position. Snapping to
    the nearest sample instead — the obvious implementation — inherits the full
    423 ms worst-case error phase 0 measured on a moving cursor.
    """
    if not moves:
        raise ValueError("no cursor samples to position from")
    index = bisect_left([m.t for m in moves], t)
    if index <= 0:
        return moves[0].x, moves[0].y
    if index >= len(moves):
        return moves[-1].x, moves[-1].y
    before, after = moves[index - 1], moves[index]
    span = after.t - before.t
    if span <= 0 or span * 1000.0 > HOLD_AFTER_MS:
        return before.x, before.y
    ratio = (t - before.t) / span
    return (
        before.x + (after.x - before.x) * ratio,
        before.y + (after.y - before.y) * ratio,
    )


def resample(moves: list[Sample], duration: float, hz: float = RESAMPLE_HZ) -> list[Sample]:
    """Cap's event-driven samples onto a fixed grid over the whole source.

    The grid spans the source rather than the samples, so a take that ends with
    the cursor parked still carries points to its last second. Absence becomes a
    run of identical positions, which is what dwell looks like to the classifier —
    one dwell rule, reached two ways, rather than two rules that drift.
    """
    if not moves or duration <= 0:
        return []
    step = 1.0 / hz
    count = int(duration / step) + 1
    grid: list[Sample] = []
    for index in range(count):
        t = min(index * step, duration)
        x, y = position_at(moves, t)
        grid.append(Sample(t=t, x=x, y=y))
    return grid


def to_focus_track(take: CapTake, duration: float, *, hz: float = RESAMPLE_HZ) -> FocusTrack:
    """The whole adapter: grid, then the classifier every adapter shares."""
    grid = resample(take.moves, duration, hz)
    # One grid step, so a click marks the sample it landed on. The measured Cap
    # rate is irrelevant here — the grid's cadence is the one the classifier sees.
    return classify(grid, take.clicks, click_window_s=(1.0 / hz) / 2.0)


SOURCE_DIR = Path("source")
"""Where a job keeps its copy of the take.

A job directory has to survive being moved or archived into `golden/`
(`spec/source.py`), so the media it renders lives inside it. The `.cap` bundle
stays where the recorder put it and is never written to."""


def ingest(
    recording: Path | str,
    job_dir: Path | str,
    *,
    source_id: str = "take",
    segment: int = 0,
) -> tuple[Source, FocusTrack]:
    """A Cap bundle in, the two `EditSpec` fields ingest owns out.

    Media facts come from `ffprobe` (`verify.probe`) and not from
    `recording-meta.json`, which claimed 25 fps over a 59 fps stream in the
    measured take. Re-reading the container here rather than trusting the sidecar
    is the same discipline `verify` applies to a render: the file is the fact.
    """
    job_dir = Path(job_dir)
    take = read_take(recording, segment)
    facts = probe(take.video)
    synthetic = (Path(recording) / SYNTHETIC_MARKER).exists()

    (job_dir / SOURCE_DIR).mkdir(parents=True, exist_ok=True)
    video = SOURCE_DIR / f"{source_id}.mp4"
    cursor = SOURCE_DIR / f"{source_id}.cursor.json"
    shutil.copy2(take.video, job_dir / video)
    shutil.copy2(take.cursor, job_dir / cursor)

    source = Source(
        source_id=source_id,
        provenance=Provenance.SYNTHETIC if synthetic else Provenance.RECORDED,
        path=str(video),
        events_path=str(cursor),
        duration=facts.duration,
        width=facts.width,
        height=facts.height,
        fps=facts.fps,
        has_audio=facts.has_audio,
    )
    return source, to_focus_track(take, facts.duration)
