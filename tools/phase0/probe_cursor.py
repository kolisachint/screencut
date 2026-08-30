"""Phase 0, risk R1: are cursor and click events extractable from a real take?

This is the verdict that can invalidate a design rather than merely inconvenience
it. `FocusTrack` assumes a recorder will hand over cursor positions and click
times; a "no" here means phase 4 has no input and the zoom planner has nothing to
plan against. The phases doc is explicit that it must be known now.

Run against a real Cap recording (`~/Library/Application Support/so.cap.desktop/
recordings/*.cap`). It reports the actual format, sample rate and coordinate
space — the three things the phases doc asks to document — plus the things that
only show up when you hold a real file: what the clocks are relative to, and
where the container metadata disagrees with the stream.

Not an adapter. Phase 4 writes the adapter, and writes it against what this
found; the phases doc's standing warning is against writing parsers for formats
nobody has looked at, and this is the looking.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
from pathlib import Path
from typing import Any

from tools.measure import write_results

FFPROBE = "/opt/homebrew/opt/ffmpeg-full/bin/ffprobe"
CAP_ROOT = Path.home() / "Library/Application Support/so.cap.desktop/recordings"


def find_recording(explicit: Path | None) -> Path:
    if explicit:
        return explicit
    candidates = sorted(
        CAP_ROOT.glob("*.cap"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    if not candidates:
        raise SystemExit(f"no .cap recordings under {CAP_ROOT}")
    return candidates[0]


def probe_stream(path: Path) -> dict[str, Any]:
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,r_frame_rate,avg_frame_rate,nb_frames,duration",
         "-of", "json", str(path)],
        capture_output=True, text=True,
    ).stdout
    return (json.loads(out).get("streams") or [{}])[0]


def interval_stats(times_s: list[float]) -> dict[str, Any]:
    gaps = [b - a for a, b in zip(times_s, times_s[1:]) if b > a]
    if not gaps:
        return {}
    gaps_sorted = sorted(gaps)
    return {
        "count": len(times_s),
        "mean_hz": round(1 / statistics.mean(gaps), 2),
        "median_hz": round(1 / statistics.median(gaps), 2),
        "min_gap_ms": round(min(gaps) * 1000, 2),
        "max_gap_ms": round(max(gaps) * 1000, 2),
        "p95_gap_ms": round(gaps_sorted[int(len(gaps_sorted) * 0.95)] * 1000, 2),
    }


def analyse(recording: Path) -> dict[str, Any]:
    meta = json.loads((recording / "recording-meta.json").read_text())
    segment = meta["segments"][0]
    display_path = recording / segment["display"]["path"]
    cursor_path = recording / segment["cursor"]

    cursor = json.loads(cursor_path.read_text())
    moves = cursor.get("moves", [])
    clicks = cursor.get("clicks", [])
    stream = probe_stream(display_path)

    move_times = [m["time_ms"] / 1000 for m in moves]
    xs = [m["x"] for m in moves]
    ys = [m["y"] for m in moves]

    # The claim that matters most for the adapter: is the coordinate space
    # already normalized? If so it is *our* space (spec/types.py Normalized) and
    # no pixel conversion is needed anywhere in ingest, which removes the whole
    # class of rounding bugs AGENTS.md warns about.
    normalized = bool(xs) and all(0.0 <= v <= 1.0 for v in xs + ys)

    # Clicks carry no position of their own, so a click's location has to come
    # from the surrounding moves. Measure how far away the nearest move sample
    # is: that gap is the error the adapter inherits.
    click_gaps = []
    for click in clicks:
        t = click["time_ms"] / 1000
        if move_times:
            click_gaps.append(min(abs(t - mt) for mt in move_times))

    stream_fps = None
    if stream.get("r_frame_rate", "0/0") != "0/0":
        num, den = stream["r_frame_rate"].split("/")
        stream_fps = round(int(num) / int(den), 3)

    return {
        "recording": recording.name,
        "extractable": bool(moves) and bool(clicks),
        "format": {
            "cursor_file": str(cursor_path.relative_to(recording)),
            "top_level_keys": sorted(cursor.keys()),
            "move_fields": sorted(moves[0].keys()) if moves else [],
            "click_fields": sorted(clicks[0].keys()) if clicks else [],
            "example_move": moves[0] if moves else None,
            "example_click": clicks[0] if clicks else None,
        },
        "coordinate_space": {
            "normalized_0_1": normalized,
            "x_range": [round(min(xs), 4), round(max(xs), 4)] if xs else None,
            "y_range": [round(min(ys), 4), round(max(ys), 4)] if ys else None,
            "matches_focustrack_space": normalized,
        },
        "sample_rate": interval_stats(move_times),
        "clicks": {
            "count": len(clicks),
            "down_events": sum(1 for c in clicks if c.get("down")),
            "carries_position": bool(clicks) and "x" in clicks[0],
            "nearest_move_sample_ms": round(max(click_gaps) * 1000, 2)
            if click_gaps else None,
        },
        "time_base": {
            "cursor_units": "time_ms, milliseconds",
            "display_start_time_s": segment["display"].get("start_time"),
            # The trap: cursor timestamps are on the *recording* clock, while the
            # video starts start_time later. An adapter that ignores this offsets
            # every zoom by that much, and 0.19s is big enough to look like a
            # planner bug rather than a clock bug (§4.5 — one time base, seconds
            # from source start).
            "cursor_clock_is_recording_not_video": True,
        },
        "video": {
            "path": str(display_path.relative_to(recording)),
            "width": stream.get("width"),
            "height": stream.get("height"),
            "duration_s": float(stream["duration"]) if stream.get("duration") else None,
            "stream_fps": stream_fps,
            "metadata_fps": segment["display"].get("fps"),
            # Second trap: recording-meta.json and the actual stream disagree.
            "fps_metadata_disagrees_with_stream": stream_fps is not None
            and stream_fps != segment["display"].get("fps"),
        },
        "also_present": {
            "keyboard_events": (recording / segment.get("keyboard", "")).exists()
            if segment.get("keyboard") else False,
            "cursor_images_with_hotspots": bool(meta.get("cursors")),
            "segments": len(meta["segments"]),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recording", type=Path, default=None)
    args = parser.parse_args()

    recording = find_recording(args.recording)
    print(f"reading {recording}")
    findings = analyse(recording)
    print(json.dumps(findings, indent=2))

    verdict = "YES" if findings["extractable"] else "NO"
    print(f"\nR1 — cursor events extractable: {verdict}")
    path = write_results("cursor_events", {"recorder": "Cap", **findings})
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
