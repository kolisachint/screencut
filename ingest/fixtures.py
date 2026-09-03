"""Synthetic fixture generator (decision #10, phase 1).

Fixtures come before real media so that every stage downstream can be written and
tested against something whose right answer is known. A fixture is a complete job
directory: a generated source video, a recorder event sidecar in the poorest
plausible recorder format (`ingest.events`), and an `EditSpec` with
**hand-authored** `EditDecisions` — no model, no `trim`, just the mechanism that
phase 2's compiler is built against.

The timeline is scripted rather than random, so a fixture is reproducible and can
be promoted into `golden/` unchanged:

    slot i  |<---------------- speech ---------------->|<-- silence -->|
            ^ cursor eases to this beat's target        ^ click cluster

Each beat contributes one caption block, one or two segments (two when a filler
word splits it), a filler removal, and a trailing silence removal — so removals
and segments partition the source exactly, which is the §4.4 invariant the
compiler and the verifier both lean on.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ingest.events import CursorSample, RecorderEvents, to_focus_track, write_events
from spec.audio import AudioTrack
from spec.captions import CaptionBlock, Word
from spec.edit import EditDecisions, Removal, RemovalKind, Segment, Tier
from spec.editspec import EditSpec
from spec.narration import Narration
from spec.overlays import OverlayIntent, OverlayTemplate
from spec.source import Source
from spec.types import Point


@dataclass(frozen=True)
class Beat:
    """One unit of the scripted take: some speech, a place to look, a rating."""

    words: tuple[str, ...]
    target: tuple[float, float]
    """Where the cursor goes, normalized."""
    tier: Tier
    reason: str
    filler_index: int | None = None
    """Index of a word that `trim` should later find on its own. Never 0 — a beat
    that opens with a filler would leave an empty leading segment."""
    clicks: tuple[float, ...] = (0.45,)
    """Click times as a fraction of the slot. Two entries make a click cluster."""
    label: str = ""
    """What an overlay anchored on this beat says."""


DEFAULT_BEATS: tuple[Beat, ...] = (
    Beat(
        words=("here", "is", "the", "dashboard", "we", "are", "shipping", "today"),
        target=(0.26, 0.30),
        tier=Tier.ESSENTIAL,
        reason="opening claim; the video makes no sense without it",
    ),
    Beat(
        words=("so", "um", "clicking", "through", "to", "the", "settings", "panel"),
        target=(0.72, 0.34),
        tier=Tier.SUPPORTING,
        reason="navigation the viewer can follow but does not need narrated",
        filler_index=1,
        label="Settings",
    ),
    Beat(
        words=("the", "export", "button", "is", "the", "part", "people", "miss"),
        target=(0.58, 0.42),
        tier=Tier.ESSENTIAL,
        reason="the one thing this recording exists to show",
        clicks=(0.40, 0.52),
        label="Export",
    ),
    Beat(
        words=("and", "that", "is", "the", "whole", "flow", "end", "to", "end"),
        target=(0.31, 0.62),
        tier=Tier.OPTIONAL,
        reason="sign-off; the short can drop it and lose nothing",
    ),
)

SLOT_S = 6.0
"""Seconds per beat."""

SPEECH_FRACTION = 0.72
"""How much of a slot is speech. The rest is the dead air `trim` will find (§4.6)."""

SAMPLE_HZ = 30.0
CURSOR_TRAVEL_FRACTION = 0.40
"""Fraction of a slot the cursor spends moving before it settles."""


@dataclass
class Fixture:
    """A generated job, in memory. `write` puts it on disk."""

    spec: EditSpec
    events: RecorderEvents
    beats: tuple[Beat, ...]
    slot_s: float = SLOT_S


# --- timeline arithmetic -----------------------------------------------------


def _slot_bounds(index: int, slot_s: float) -> tuple[float, float, float]:
    """(slot start, speech end, slot end) for a beat."""
    start = index * slot_s
    return start, start + slot_s * SPEECH_FRACTION, start + slot_s


def _word_spans(beat: Beat, index: int, slot_s: float) -> list[tuple[float, float]]:
    start, speech_end, _ = _slot_bounds(index, slot_s)
    width = (speech_end - start) / len(beat.words)
    return [(start + k * width, start + (k + 1) * width) for k in range(len(beat.words))]


def build_captions(beats: tuple[Beat, ...], slot_s: float = SLOT_S) -> list[CaptionBlock]:
    """One block per beat, carrying per-word timings from the start (§6.2).

    The filler word gets a span identical to the removal that will take it out, so
    a compiler trimming a caption at a cut boundary has an exact case to get right
    rather than an approximate one.
    """
    blocks: list[CaptionBlock] = []
    for index, beat in enumerate(beats):
        start, speech_end, _ = _slot_bounds(index, slot_s)
        spans = _word_spans(beat, index, slot_s)
        blocks.append(
            CaptionBlock(
                t_in=start,
                t_out=speech_end,
                words=[Word(t_in=a, t_out=b, text=w) for w, (a, b) in zip(beat.words, spans)],
            )
        )
    return blocks


def build_edit_decisions(beats: tuple[Beat, ...], slot_s: float = SLOT_S) -> EditDecisions:
    """Hand-authored, and total: every second is removed or in a segment (§4.4)."""
    removals: list[Removal] = []
    segments: list[Segment] = []
    for index, beat in enumerate(beats):
        start, speech_end, slot_end = _slot_bounds(index, slot_s)
        spans = _word_spans(beat, index, slot_s)
        if beat.filler_index is not None:
            filler_in, filler_out = spans[beat.filler_index]
            segments.append(Segment(t_in=start, t_out=filler_in, tier=beat.tier, reason=beat.reason))
            removals.append(
                Removal(t_in=filler_in, t_out=filler_out, kind=RemovalKind.FILLER, proposed_by="trim")
            )
            segments.append(
                Segment(t_in=filler_out, t_out=speech_end, tier=beat.tier, reason=beat.reason)
            )
        else:
            segments.append(Segment(t_in=start, t_out=speech_end, tier=beat.tier, reason=beat.reason))
        removals.append(
            Removal(t_in=speech_end, t_out=slot_end, kind=RemovalKind.SILENCE, proposed_by="trim")
        )
    return EditDecisions(removals=removals, segments=segments)


def build_overlays(beats: tuple[Beat, ...], slot_s: float = SLOT_S) -> list[OverlayIntent]:
    """One anchored overlay per beat that can take one, plus one that spans the output.

    One of them is deliberately placed inside the first silence removal.
    `plan_overlays` sees material that will later be cut and may waste an overlay on
    it; compile drops it deterministically (§4.5), and a fixture that never
    exercises that path lets the bug live until real footage finds it.
    """
    overlays: list[OverlayIntent] = []
    anchored = (OverlayTemplate.LABEL_CHIP, OverlayTemplate.HIGHLIGHT_BOX)
    for offset, template in enumerate(anchored):
        index = offset + 1
        if index >= len(beats):
            break
        beat = beats[index]
        start, speech_end, _ = _slot_bounds(index, slot_s)
        overlays.append(
            OverlayIntent(
                template=template,
                text=beat.label or " ".join(beat.words[-2:]).title(),
                anchor=Point(x=beat.target[0], y=beat.target[1]),
                t_in=start + slot_s * 0.08,
                t_out=speech_end,
            )
        )
    _, speech_end_0, slot_end_0 = _slot_bounds(0, slot_s)
    gap = slot_end_0 - speech_end_0
    overlays.append(
        OverlayIntent(
            template=OverlayTemplate.CALLOUT_ARROW,
            text="dropped by compile: this lands inside a removal",
            anchor=Point(x=0.5, y=0.5),
            t_in=speech_end_0 + gap * 0.1,
            t_out=slot_end_0 - gap * 0.1,
        )
    )
    overlays.append(OverlayIntent(template=OverlayTemplate.PROGRESS_PILL))
    return overlays


def build_events(
    beats: tuple[Beat, ...],
    *,
    width: int,
    height: int,
    slot_s: float = SLOT_S,
    sample_hz: float = SAMPLE_HZ,
) -> RecorderEvents:
    """A scripted cursor path in the recorder's own units: pixels and timestamps.

    Eases to each beat's target, then rests there with a small deterministic
    wobble — enough movement to be realistic, little enough that the adapter
    classifies it as dwell.
    """
    duration = len(beats) * slot_s
    samples: list[CursorSample] = []
    clicks: list[float] = []
    previous = beats[-1].target
    for index, beat in enumerate(beats):
        start, _, _ = _slot_bounds(index, slot_s)
        origin = previous if index else (0.5, 0.5)
        travel = slot_s * CURSOR_TRAVEL_FRACTION
        step = 1.0 / sample_hz
        t = start
        while t < start + slot_s - 1e-9:
            progress = min((t - start) / travel, 1.0)
            eased = progress * progress * (3.0 - 2.0 * progress)  # smoothstep
            x = origin[0] + (beat.target[0] - origin[0]) * eased
            y = origin[1] + (beat.target[1] - origin[1]) * eased
            if progress >= 1.0:  # resting, but a hand is never perfectly still
                x += 0.002 * math.sin(t * 7.3)
                y += 0.002 * math.cos(t * 5.1)
            samples.append(CursorSample(t=round(t, 4), x=round(x * width, 2), y=round(y * height, 2)))
            t += step
        clicks.extend(round(start + fraction * slot_s, 4) for fraction in beat.clicks)
        previous = beat.target
    return RecorderEvents(width=width, height=height, duration=duration, samples=samples, clicks=clicks)


def build_spec(
    job_id: str = "fixture-demo",
    *,
    beats: tuple[Beat, ...] = DEFAULT_BEATS,
    width: int = 1920,
    height: int = 1080,
    fps: float = 30.0,
    slot_s: float = SLOT_S,
    source_path: str = "source/source.mp4",
    events_path: str = "source/events.json",
) -> Fixture:
    events = build_events(beats, width=width, height=height, slot_s=slot_s)
    spec = EditSpec(
        job_id=job_id,
        # Fixed so a fixture serializes identically on every machine — a golden
        # spec that differs only by timestamp is a diff nobody can read (§11).
        created_at="2026-01-01T00:00:00Z",
        source=Source(
            source_id="synthetic",
            path=source_path,
            events_path=events_path,
            duration=len(beats) * slot_s,
            width=width,
            height=height,
            fps=fps,
        ),
        narration=Narration(),
        focus=to_focus_track(events),
        edit=build_edit_decisions(beats, slot_s),
        captions=build_captions(beats, slot_s),
        overlays=build_overlays(beats, slot_s),
        audio=AudioTrack(),
    )
    return Fixture(spec=spec, events=events, beats=beats, slot_s=slot_s)


# --- the deliberately bad fixture -------------------------------------------

LONG_WORD = "supercalifragilisticexpialidocious"


def break_fixture(fixture: Fixture) -> Fixture:
    """Damage a good fixture in the ways a *spec* can express (§11).

    §9's checks have to be tested against known-bad rather than only known-good, or
    a check that never fires is indistinguishable from one that cannot. Three
    breakages, chosen because the schema still permits them:

    - a cut whose edges land inside words, which clips them — invisible in a still
      frame and audible immediately,
    - a caption word too long to wrap into the profile's line length,
    - an overlay anchored where the caption box will be, so it occludes one.

    Two of §11's four — overlapping captions and a juddering crop — are *not* here,
    and deliberately: `EditSpec` refuses overlapping caption blocks and `plan_focus`
    rate-limits the crop by construction, so neither is representable. Their checks
    are exercised in tests against hand-built inputs, which is the honest place for
    a check on something the pipeline cannot produce.
    """
    spec = fixture.spec
    filler = next(r for r in spec.edit.removals if r.kind is RemovalKind.FILLER)
    word = (spec.source.duration / len(fixture.beats)) * SPEECH_FRACTION / len(fixture.beats[1].words)
    shift = word / 2

    removals = [
        r.model_copy(update={"t_in": r.t_in + shift, "t_out": r.t_out + shift}) if r is filler else r
        for r in spec.edit.removals
    ]
    segments = []
    for segment in spec.edit.segments:
        if abs(segment.t_out - filler.t_in) < TIME_NUDGE:
            segment = segment.model_copy(update={"t_out": segment.t_out + shift})
        elif abs(segment.t_in - filler.t_out) < TIME_NUDGE:
            segment = segment.model_copy(update={"t_in": segment.t_in + shift})
        segments.append(segment)

    captions = list(spec.captions)
    first = captions[0]
    captions[0] = first.model_copy(
        update={"words": [first.words[0].model_copy(update={"text": LONG_WORD})] + list(first.words[1:])}
    )

    _, speech_end_0, _ = _slot_bounds(0, fixture.slot_s)
    overlays = list(spec.overlays) + [
        OverlayIntent(
            template=OverlayTemplate.LABEL_CHIP,
            text="sits on the caption",
            anchor=Point(x=0.5, y=0.78),
            t_in=0.5,
            t_out=speech_end_0,
        )
    ]
    broken = spec.model_copy(
        update={
            "job_id": f"{spec.job_id}-broken",
            "edit": EditDecisions(removals=removals, segments=segments),
            "captions": captions,
            "overlays": overlays,
        }
    )
    return Fixture(spec=broken, events=fixture.events, beats=fixture.beats, slot_s=fixture.slot_s)


TIME_NUDGE = 1e-6


# --- the generated source video ---------------------------------------------

_BOX_COLORS = ("cyan", "orange", "magenta", "yellow")


def source_ffmpeg_command(fixture: Fixture, out_path: Path, *, silent: bool = False) -> list[str]:
    """FFmpeg arguments for the fixture's source video.

    `silent` writes no audio stream at all, which is what a screen capture with
    the mic off looks like and what phase 8's narrated fixture needs: the
    narration is a separate file, and a tone under it would be neither the
    recording's audio nor the narration.

    `testsrc2` gives motion in every frame; a coloured box sits at each beat's
    cursor target while that beat is on screen, so "the zoom landed on the click
    cluster" is a thing you can check by eye rather than by trusting the maths.
    The tone drops to silence in the gaps, which is the dead air `trim` is
    supposed to find later (§4.6).
    """
    source = fixture.spec.source
    duration = source.duration
    box_w, box_h = int(source.width * 0.18), int(source.height * 0.14)
    boxes = []
    silences = []
    for index, beat in enumerate(fixture.beats):
        start, speech_end, slot_end = _slot_bounds(index, fixture.slot_s)
        x = int(beat.target[0] * source.width - box_w / 2)
        y = int(beat.target[1] * source.height - box_h / 2)
        colour = _BOX_COLORS[index % len(_BOX_COLORS)]
        boxes.append(
            f"drawbox=x={x}:y={y}:w={box_w}:h={box_h}:color={colour}@0.75:t=fill"
            f":enable='between(t,{start},{slot_end})'"
        )
        silences.append(f"between(t,{speech_end},{slot_end})")
    video_chain = ",".join(boxes) if boxes else "null"
    audio_chain = f"volume=volume=0:enable='{'+'.join(silences)}'" if silences else "anull"
    audio_inputs = [] if silent else [
        "-f", "lavfi",
        "-i", f"sine=frequency=220:sample_rate=48000:duration={duration}",
    ]
    graph = f"[0:v]{video_chain}[v]" + ("" if silent else f";[1:a]{audio_chain}[a]")
    audio_output = [] if silent else ["-map", "[a]", "-c:a", "aac", "-b:a", "192k"]
    return [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        # Bit-exact and metadata-free: a fixture that changes bytes between runs
        # cannot be a golden fixture (§11, §16).
        "-fflags", "+bitexact",
        "-flags", "+bitexact",
        "-f", "lavfi",
        "-i", f"testsrc2=size={source.width}x{source.height}:rate={source.fps}:duration={duration}",
        *audio_inputs,
        "-filter_complex", graph,
        "-map", "[v]",
        *audio_output,
        "-map_metadata", "-1",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-shortest",
        str(out_path),
    ]


class FfmpegMissing(RuntimeError):
    pass


def render_source(fixture: Fixture, out_path: Path, *, silent: bool = False) -> Path:
    if shutil.which("ffmpeg") is None:
        raise FfmpegMissing("ffmpeg is not on PATH; pass --no-video to generate the spec alone")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(source_ffmpeg_command(fixture, out_path, silent=silent), check=True)
    return out_path


# --- writing a job directory -------------------------------------------------


def write_fixture(out_dir: Path, fixture: Fixture, *, with_video: bool = True) -> Path:
    """Write a job directory in the §5.4 layout."""
    out_dir = Path(out_dir)
    (out_dir / "source").mkdir(parents=True, exist_ok=True)
    (out_dir / "stages").mkdir(exist_ok=True)
    (out_dir / "renders").mkdir(exist_ok=True)

    write_events(fixture.events, out_dir / fixture.spec.source.events_path)
    (out_dir / "spec.json").write_text(
        json.dumps(fixture.spec.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    )
    if with_video:
        render_source(fixture, out_dir / fixture.spec.source.path)
    return out_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a synthetic screencut fixture job.")
    parser.add_argument("--out", default="data/fixtures/demo01", help="Job directory to write.")
    parser.add_argument("--job-id", default="fixture-demo")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--no-video", action="store_true", help="Write the spec and events, skip ffmpeg.")
    parser.add_argument("--broken", action="store_true",
                        help="Damage the fixture in the ways §11 wants tested against known-bad.")
    args = parser.parse_args(argv)

    fixture = build_spec(args.job_id, width=args.width, height=args.height, fps=args.fps)
    if args.broken:
        fixture = break_fixture(fixture)
    out = write_fixture(Path(args.out), fixture, with_video=not args.no_video)
    print(f"{out}  ({fixture.spec.source.duration:.1f}s, {len(fixture.spec.edit.segments)} segments, "
          f"{len(fixture.spec.edit.removals)} removals)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
