"""Measuring a rendered file.

Everything here reads the render rather than the spec, which is the point: the
spec says what was asked for, and these say what came out. A check that compares
the spec to itself catches nothing.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MediaFacts:
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool
    bytes: int


@dataclass(frozen=True)
class Loudness:
    """EBU R128, as §9.1 asks for it."""

    integrated_lufs: float
    true_peak_dbtp: float
    range_lu: float


def probe(path: Path) -> MediaFacts:
    output = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,width,height,avg_frame_rate",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    data = json.loads(output)
    video = next(s for s in data["streams"] if s["codec_type"] == "video")
    return MediaFacts(
        duration=float(data["format"]["duration"]),
        width=int(video["width"]),
        height=int(video["height"]),
        fps=_ratio(video.get("avg_frame_rate", "0/1")),
        has_audio=any(s["codec_type"] == "audio" for s in data["streams"]),
        bytes=Path(path).stat().st_size,
    )


def _ratio(value: str) -> float:
    numerator, _, denominator = value.partition("/")
    try:
        return float(numerator) / float(denominator or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0


_SUMMARY = re.compile(
    r"Integrated loudness:.*?I:\s*(?P<i>-?\d+\.?\d*)\s*LUFS"
    r".*?LRA:\s*(?P<lra>-?\d+\.?\d*)\s*LU"
    r".*?True peak:.*?Peak:\s*(?P<peak>-?\d+\.?\d*)\s*dBFS",
    re.DOTALL,
)


def measure_loudness(path: Path) -> Loudness | None:
    """Scan the whole file with `ebur128`.

    A single pass over the audio, and the same standard the render normalized to,
    so the check is measuring the thing rather than re-deriving it. Returns None
    when there is no audio to measure.
    """
    completed = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128=peak=true", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    match = _SUMMARY.search(completed.stderr)
    if not match:
        return None
    return Loudness(
        integrated_lufs=float(match["i"]),
        true_peak_dbtp=float(match["peak"]),
        range_lu=float(match["lra"]),
    )
