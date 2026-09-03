"""A narrated job: a silent screen capture, a script, and a voice reference (phase 8).

The synthetic fixture of `ingest/fixtures.py` is a *recorded* take — it carries
hand-authored captions and an edit, because phase 2 needed something whose right
answer was known. This one is the other shape the pipeline supports: a screen
capture with the mic off, plus a script to read over it in your own voice
(decision #20). Its spec deliberately carries **no captions and no edit**,
because on this path those come from `align` and `plan_edit`, and a fixture that
supplied them would be testing the compiler against itself.

Same job as `ingest/cap_fixture.py` does for phase 4, and the same honesty about
its limits:

- **The voice reference is a tone.** Cloning a tone proves the invocation, the
  file handling and the schema boundary; it proves nothing about whether the
  result sounds like you. That half needs a person and a microphone, exactly as
  phase 4's ASR-on-a-test-tone needed real speech.
- **Nothing here runs without the two backends.** `tts` wants F5-TTS and `align`
  wants `whisper-cli`, and without them the job fails saying so — which is the
  same contract `make take` has.

What it *does* prove, and what `make narrate` is for: a script goes in at one end
and a captioned video comes out at the other, with the narration as the audio
spine and §9.2 listening to the result.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from ingest.events import to_focus_track, write_events
from ingest.fixtures import Beat, Fixture, SLOT_S, build_events, render_source
from runner.job import JobConfig
from runner.stages import SYNTHESIZED_STAGES
from spec.audio import AudioTrack
from spec.editspec import EditSpec
from spec.edit import Tier
from spec.narration import Narration, NarrationSource
from spec.source import Source

SCRIPT = (
    "Here is the analytics dashboard we shipped this week. "
    "Every panel reads from the same query, so the numbers on the left and the "
    "chart on the right can never disagree. "
    "The export button is the part people miss. It writes exactly what is on "
    "screen, filters included. "
    "That is the whole flow, end to end."
)
"""Roughly twenty seconds read aloud, against a twenty-four second recording.

Deliberately shorter than the video. A narration that overruns its screen
capture is a real mistake and `align` fails on it by name; a fixture that sat on
that boundary would fail intermittently for a reason that has nothing to do with
what it is testing."""

VOICE_REFERENCE_TEXT = "This is my voice, recorded for this job and for nothing else."
"""What the reference clip says. F5-TTS conditions on it (`synth/tts.py`)."""

CONSENT = "Synthetic fixture: the reference is a generated tone, not a person."
"""Decision #20 wants the boundary auditable rather than assumed, and a fixture
is exactly where a consent note could get copied into a real job without being
read. So this one says what it is."""

BEATS: tuple[Beat, ...] = (
    Beat(words=(), target=(0.26, 0.30), tier=Tier.ESSENTIAL, reason="", clicks=(0.45,)),
    Beat(words=(), target=(0.72, 0.34), tier=Tier.ESSENTIAL, reason="", clicks=(0.45,)),
    Beat(words=(), target=(0.58, 0.42), tier=Tier.ESSENTIAL, reason="", clicks=(0.40, 0.52)),
    Beat(words=(), target=(0.31, 0.62), tier=Tier.ESSENTIAL, reason="", clicks=(0.45,)),
)
"""Cursor movement only. The words, the tiers and the reasons belong to the
stages that have not run yet — carrying them here would be the fixture answering
the questions it exists to ask."""

REFERENCE_SECONDS = 8.0


def voice_reference_command(out_path: Path, seconds: float = REFERENCE_SECONDS) -> list[str]:
    """A stand-in reference clip, at F5-TTS's own sample rate.

    A tone rather than speech, and `synth/tts.py`'s floor on reference length is
    what decides how long it is. Byte-stable like every other fixture asset (§11).
    """
    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-fflags", "+bitexact", "-flags", "+bitexact",
        "-f", "lavfi", "-i", f"sine=frequency=180:sample_rate=24000:duration={seconds:g}",
        "-ac", "1", "-c:a", "pcm_s16le", "-map_metadata", "-1",
        str(out_path),
    ]


def build_narrated_spec(
    job_id: str = "fixture-narrated",
    *,
    beats: tuple[Beat, ...] = BEATS,
    width: int = 1280,
    height: int = 720,
    fps: float = 30.0,
    slot_s: float = SLOT_S,
    script: str = SCRIPT,
) -> Fixture:
    events = build_events(beats, width=width, height=height, slot_s=slot_s)
    spec = EditSpec(
        job_id=job_id,
        created_at="2026-01-01T00:00:00Z",
        source=Source(
            source_id="synthetic-narrated",
            path="source/source.mp4",
            events_path="source/events.json",
            duration=len(beats) * slot_s,
            width=width,
            height=height,
            fps=fps,
            # The mic was off. That is the ordinary shape of a screen capture you
            # intend to narrate afterwards, and it is why `narration_path` exists.
            has_audio=False,
        ),
        narration=Narration(
            source=NarrationSource.SYNTHESIZED,
            script=script,
            voice_reference_path="source/voice.wav",
            voice_reference_text=VOICE_REFERENCE_TEXT,
            voice_consent_note=CONSENT,
        ),
        focus=to_focus_track(events),
        audio=AudioTrack(),
    )
    return Fixture(spec=spec, events=events, beats=beats, slot_s=slot_s)


def write_narrated_fixture(out_dir: Path, fixture: Fixture, *, with_video: bool = True) -> Path:
    """Write the job directory, and the `job.json` that says which recipe it is."""
    out_dir = Path(out_dir)
    (out_dir / "source").mkdir(parents=True, exist_ok=True)
    (out_dir / "stages").mkdir(exist_ok=True)
    (out_dir / "renders").mkdir(exist_ok=True)

    write_events(fixture.events, out_dir / fixture.spec.source.events_path)
    (out_dir / "spec.json").write_text(fixture.spec.model_dump_json(indent=2) + "\n")
    JobConfig(stages=list(SYNTHESIZED_STAGES), recorder="synthetic").write(out_dir)
    if with_video:
        render_source(fixture, out_dir / fixture.spec.source.path, silent=True)
        subprocess.run(
            voice_reference_command(out_dir / fixture.spec.narration.voice_reference_path),
            check=True,
        )
    return out_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="data/fixtures/narrated01", help="Job directory to write.")
    parser.add_argument("--job-id", default="fixture-narrated")
    parser.add_argument("--no-video", action="store_true", help="Write the spec and events, skip ffmpeg.")
    args = parser.parse_args(argv)

    fixture = build_narrated_spec(args.job_id)
    out = write_narrated_fixture(Path(args.out), fixture, with_video=not args.no_video)
    words = len(fixture.spec.narration.script.split())
    print(f"{fixture.spec.job_id}: {fixture.spec.source.duration:.0f}s of silent capture")
    print(f"  script: {words} words, to be read in the voice at {fixture.spec.narration.voice_reference_path}")
    print(f"  stages: {' -> '.join(SYNTHESIZED_STAGES)}")
    print(f"  wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
