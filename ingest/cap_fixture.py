"""A synthetic take in Cap's on-disk format (phase 4).

Phase 4's exit criteria are written against a *real* recording, and one is not
available on every machine this repository is worked on — it needs a screen, a
microphone and Cap. This writes a `.cap` bundle carrying the same scripted beats
as `ingest.fixtures`, so `screencut ingest` and everything downstream of it can be
run end to end without one.

It is a stand-in for a real take, not a substitute for one. What it does prove is
the part a real take proves badly: that the adapter handles the four traps phase 0
measured, because every one of them is deliberately in here at or beyond the
measured severity —

- **the cursor emits nothing while it rests**, leaving a 3.6 s gap against the
  1.98 s worst case in the real take,
- **clicks land inside those gaps** and carry no position of their own,
- **cursor times are on the recording clock**, offset by the measured 0.194 s,
- **`recording-meta.json` states an fps the stream does not have**, 25 against 30,
  which is the same disagreement the real take had at 25 against 59.

A fixture whose traps are gentler than reality is a fixture that passes while the
adapter is broken.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from ingest.fixtures import (
    CURSOR_TRAVEL_FRACTION,
    DEFAULT_BEATS,
    SLOT_S,
    Beat,
    _slot_bounds,
    build_spec,
    render_source,
)

START_TIME_S = 0.194069708
"""The measured `display.start_time` from the real take. Cursor timestamps run on
the recording clock and the video begins this much later."""

META_FPS = 25
"""What the sidecar claims. The stream is 30. The real take claimed 25 over 59."""

EMIT_HZ = 37.0
"""Cap's measured *median* rate — while the cursor is moving, which is the only
time it emits at all."""


def cursor_events(
    beats: tuple[Beat, ...] = DEFAULT_BEATS, slot_s: float = SLOT_S
) -> dict[str, list[dict]]:
    """Cap's `cursor.json`: normalized moves while travelling, silence while resting.

    The silence is the point. Sampling on movement means a resting cursor is
    absent from the file rather than repeated in it, and an adapter that reads
    absence as missing data interpolates a glide across the one part of the take
    where the user was definitely looking at something.
    """
    moves: list[dict] = []
    clicks: list[dict] = []
    previous = beats[-1].target
    step = 1.0 / EMIT_HZ

    for index, beat in enumerate(beats):
        start, _, _ = _slot_bounds(index, slot_s)
        origin = previous if index else (0.5, 0.5)
        travel = slot_s * CURSOR_TRAVEL_FRACTION
        t = start
        while t <= start + travel + 1e-9:
            progress = min((t - start) / travel, 1.0)
            eased = progress * progress * (3.0 - 2.0 * progress)  # smoothstep
            moves.append(
                {
                    "time_ms": round((t + START_TIME_S) * 1000, 6),
                    "x": round(origin[0] + (beat.target[0] - origin[0]) * eased, 7),
                    "y": round(origin[1] + (beat.target[1] - origin[1]) * eased, 7),
                    "cursor_id": "1",
                    "active_modifiers": [],
                }
            )
            t += step
        # Nothing between here and the next beat: the hand has stopped moving.
        for fraction in beat.clicks:
            at = start + fraction * slot_s
            clicks.append(
                {
                    "time_ms": round((at + START_TIME_S) * 1000, 6),
                    "down": True,
                    "cursor_num": 1,
                    "cursor_id": "0",
                    "active_modifiers": [],
                }
            )
            clicks.append(
                {
                    "time_ms": round((at + 0.08 + START_TIME_S) * 1000, 6),
                    "down": False,
                    "cursor_num": 1,
                    "cursor_id": "0",
                    "active_modifiers": [],
                }
            )
        previous = beat.target

    return {"moves": moves, "clicks": clicks}


def write_bundle(
    out: Path | str,
    *,
    beats: tuple[Beat, ...] = DEFAULT_BEATS,
    width: int = 1920,
    height: int = 1080,
    fps: float = 30.0,
    with_video: bool = True,
) -> Path:
    """Write a `.cap` directory. Byte-stable, like every fixture here (§11)."""
    root = Path(out)
    segment = root / "content" / "segments" / "segment-0"
    segment.mkdir(parents=True, exist_ok=True)

    (segment / "cursor.json").write_text(
        json.dumps(cursor_events(beats), indent=2, sort_keys=True) + "\n"
    )
    (root / "recording-meta.json").write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "display": {
                            "path": "content/segments/segment-0/display.mp4",
                            "start_time": START_TIME_S,
                            "fps": META_FPS,
                        },
                        "cursor": "content/segments/segment-0/cursor.json",
                    }
                ]
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    if with_video:
        # The same generator the synthetic job uses, so "the zoom landed on the
        # click cluster" is still checkable by eye: a coloured box sits at each
        # beat's target while that beat is on screen.
        fixture = build_spec("cap-fixture", beats=beats, width=width, height=height, fps=fps)
        render_source(fixture, segment / "display.mp4")
    return root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="data/fixtures/take01.cap")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--no-video", action="store_true")
    args = parser.parse_args(argv)

    root = write_bundle(
        args.out, width=args.width, height=args.height, fps=args.fps, with_video=not args.no_video
    )
    events = json.loads((root / "content/segments/segment-0/cursor.json").read_text())
    gaps = [
        b["time_ms"] - a["time_ms"] for a, b in zip(events["moves"], events["moves"][1:])
    ]
    print(f"{root}  {len(events['moves'])} moves, {len(events['clicks'])} click events")
    print(f"  longest gap {max(gaps):.0f}ms (the real take's worst was 1981ms)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
