"""Phase 0: the two encode paths, timed against each other.

`h264_videotoolbox` is the target machine's hardware path and `libx264` is what
the golden set pays for on every replay (phases doc, phase 0), so the number that
matters is not "is hardware faster" — it is *how much* the reproducible path
costs, because that cost is paid on every golden run rather than once.

Both paths are run three times back to back. On a fanless M1 Air that is the whole
point: VideoToolbox is a fixed-function block and holds its speed, `libx264` is
eight cores of general compute and does not, so burst and sustained diverge for
one of them and not the other. Reporting a single number would hide which.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from tools.measure import Measurement, repeat, report, run_once, write_results

#: Homebrew's plain `ffmpeg` formula has no libass (see the findings document),
#: so the project needs the keg-only `ffmpeg-full`. Resolved here rather than
#: assumed, because "ffmpeg was on PATH" is exactly the assumption phase 0 exists
#: to replace.
FFMPEG_CANDIDATES = [
    "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg",
    "ffmpeg",
]


def resolve_ffmpeg() -> str:
    for candidate in FFMPEG_CANDIDATES:
        path = shutil.which(candidate) or (
            candidate if Path(candidate).exists() else None
        )
        if not path:
            continue
        return path
    raise SystemExit("no ffmpeg found")


def make_source(ffmpeg: str, path: Path, seconds: int) -> None:
    """A minute of 1080p30 with real motion.

    `testsrc2` rather than a still: a static source lets both encoders cheat with
    skip blocks and produces a timing that says nothing about a screen recording.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    run_once(
        "make-source",
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"testsrc2=size=1920x1080:rate=30:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k", "-pix_fmt", "yuv420p",
            str(path),
        ],
    )


def encode_cmd(ffmpeg: str, src: Path, dst: Path, encoder: str) -> list[str]:
    common = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src)]
    if encoder == "videotoolbox":
        codec = ["-c:v", "h264_videotoolbox", "-b:v", "8M"]
    else:
        # Mirrors runner/render's software path: bitexact, so two runs are
        # byte-identical (phase 2's exit criterion).
        codec = [
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-fflags", "+bitexact", "-flags", "+bitexact",
        ]
    return common + codec + ["-pix_fmt", "yuv420p", "-c:a", "copy", str(dst)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--workdir", type=Path, default=Path("/tmp/phase0/encode"))
    args = parser.parse_args()

    ffmpeg = resolve_ffmpeg()
    src = args.workdir / "source.mp4"
    if not src.exists():
        print(f"generating {args.seconds}s 1080p30 source ...")
        make_source(ffmpeg, src, args.seconds)

    measurements: list[Measurement] = []
    for encoder in ("videotoolbox", "software"):
        dst = args.workdir / f"out_{encoder}.mp4"
        measurements.append(
            repeat(
                f"encode/{encoder}",
                encode_cmd(ffmpeg, src, dst, encoder),
                repeats=args.repeats,
            )
        )

    print(report(measurements))

    hw, sw = measurements[0], measurements[1]
    realtime = {
        m.label: round(args.seconds / m.sustained_s, 2) for m in measurements
    }
    payload = {
        "ffmpeg": ffmpeg,
        "source_seconds": args.seconds,
        "realtime_factor_sustained": realtime,
        "software_cost_multiple": round(sw.sustained_s / hw.sustained_s, 2),
        "measurements": [m.to_dict() for m in measurements],
    }
    path = write_results("encode", payload)
    print(f"\nsoftware costs {payload['software_cost_multiple']}x hardware")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
