"""The Cap adapter, against the four traps phase 0 measured.

Every number in here came off a real take (`docs/measurements/cursor_events.json`)
rather than out of a plausible range: the 1 981 ms rest gap, the 423 ms worst
click-to-sample distance, the 0.194 s clock offset, and the sidecar claiming 25
fps over a 59 fps stream. A trap tested with a comfortable number is a trap that
still fires on the recording.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ingest.cap import HOLD_AFTER_MS, RESAMPLE_HZ, position_at, read_take, resample, to_focus_track
from ingest.events import Sample
from spec.focus import FocusKind

START_TIME = 0.194069708
"""The measured display start_time. Cursor times are on the recording clock."""


def write_bundle(
    root: Path,
    moves: list[dict],
    clicks: list[dict],
    *,
    start_time: float = START_TIME,
    meta_fps: int = 25,
) -> Path:
    """A `.cap` bundle with exactly the shape phase 0 documented."""
    segment = root / "content" / "segments" / "segment-0"
    segment.mkdir(parents=True, exist_ok=True)
    (segment / "cursor.json").write_text(json.dumps({"moves": moves, "clicks": clicks}))
    (segment / "display.mp4").write_bytes(b"")  # never probed by these tests
    (root / "recording-meta.json").write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "display": {
                            "path": "content/segments/segment-0/display.mp4",
                            "start_time": start_time,
                            "fps": meta_fps,
                        },
                        "cursor": "content/segments/segment-0/cursor.json",
                    }
                ]
            }
        )
    )
    return root


def move(time_ms: float, x: float, y: float) -> dict:
    return {"time_ms": time_ms, "x": x, "y": y, "cursor_id": "1", "active_modifiers": []}


def click(time_ms: float, down: bool = True) -> dict:
    return {"time_ms": time_ms, "down": down, "cursor_num": 1, "cursor_id": "0", "active_modifiers": []}


# --- trap 3: two clocks ------------------------------------------------------


def test_cursor_times_are_shifted_out_of_the_recording_clock_into_source_time(tmp_path):
    """§4.5 permits one time base. The offset is subtracted once, at the boundary."""
    root = write_bundle(tmp_path / "t.cap", [move(1000.0, 0.5, 0.5)], [click(1500.0)])
    take = read_take(root)
    assert take.moves[0].t == pytest.approx(1.0 - START_TIME)
    assert take.clicks[0] == pytest.approx(1.5 - START_TIME)


def test_events_from_before_the_video_started_are_dropped_not_clamped(tmp_path):
    """Clamping them to zero piles them on one point, which reads to `plan_focus`
    as an emphatic dwell on wherever the pointer happened to be parked."""
    root = write_bundle(tmp_path / "t.cap", [move(50.0, 0.1, 0.1), move(1000.0, 0.5, 0.5)], [click(60.0)])
    take = read_take(root)
    assert [round(m.t, 3) for m in take.moves] == [round(1.0 - START_TIME, 3)]
    assert take.clicks == []


def test_mouse_up_is_not_attention(tmp_path):
    root = write_bundle(tmp_path / "t.cap", [move(1000.0, 0.5, 0.5)], [click(1200.0, down=True), click(1300.0, down=False)])
    assert len(read_take(root).clicks) == 1


# --- trap 1: sampling is event-driven ----------------------------------------


def test_a_long_gap_holds_position_because_a_resting_cursor_emits_nothing():
    """The measured maximum gap is 1 981 ms. Interpolating across it invents a
    slow glide that never happened and hides the clearest dwell in the take."""
    moves = [Sample(t=0.0, x=0.2, y=0.2), Sample(t=1.981, x=0.8, y=0.8)]
    assert position_at(moves, 1.0) == (0.2, 0.2)
    assert position_at(moves, 1.98) == (0.2, 0.2)


def test_a_short_gap_interpolates_because_that_one_is_the_sampler_breathing():
    gap = (HOLD_AFTER_MS / 1000.0) * 0.5
    moves = [Sample(t=0.0, x=0.0, y=0.0), Sample(t=gap, x=1.0, y=1.0)]
    x, y = position_at(moves, gap / 2)
    assert x == pytest.approx(0.5)
    assert y == pytest.approx(0.5)


def test_resampling_covers_the_whole_source_not_just_the_sampled_part():
    """A take that ends with the cursor parked emits nothing for its last seconds,
    and a track that stops early leaves `plan_focus` planning against silence."""
    grid = resample([Sample(t=0.0, x=0.5, y=0.5), Sample(t=1.0, x=0.5, y=0.5)], duration=5.0)
    assert grid[0].t == 0.0
    assert grid[-1].t == pytest.approx(5.0)
    assert all(abs(b.t - a.t - 1 / RESAMPLE_HZ) < 1e-6 for a, b in zip(grid, grid[1:-1]))


def test_absence_of_samples_becomes_dwell_rather_than_no_data(tmp_path):
    """Phase 0's sharpened trap: dwell is not mismeasured across a gap, it is
    invisible in one. Resampling turns the gap into stationary samples, which is
    what the shared classifier already knows how to read."""
    root = write_bundle(
        tmp_path / "t.cap",
        [move(200.0 + START_TIME * 1000, 0.30, 0.30), move(2181.0 + START_TIME * 1000, 0.30, 0.30)],
        [],
    )
    track = to_focus_track(read_take(root), duration=2.5)
    resting = [p for p in track.points if 0.6 < p.t < 2.1]
    assert resting and all(p.kind is FocusKind.DWELL for p in resting)


# --- trap 2: clicks carry no position ----------------------------------------


def test_a_click_takes_its_position_from_the_samples_bracketing_it():
    """Snapping to the nearest sample inherits the full 423 ms worst-case error
    phase 0 measured, which on a moving cursor is somewhere else entirely."""
    moves = [Sample(t=0.0, x=0.0, y=0.0), Sample(t=0.10, x=1.0, y=0.0)]
    x, _ = position_at(moves, 0.09)
    assert x == pytest.approx(0.9)  # not 1.0, which is what the nearest sample says


def test_a_click_is_marked_on_the_grid_sample_it_landed_on(tmp_path):
    root = write_bundle(
        tmp_path / "t.cap",
        [move(START_TIME * 1000, 0.2, 0.2), move(1000.0 + START_TIME * 1000, 0.6, 0.6)],
        [click(500.0 + START_TIME * 1000)],
    )
    track = to_focus_track(read_take(root), duration=1.0)
    clicked = track.clicks()
    assert len(clicked) == 1
    assert clicked[0].t == pytest.approx(0.5, abs=1 / RESAMPLE_HZ)


# --- shape ------------------------------------------------------------------


def test_cap_coordinates_are_used_as_they_are_because_they_are_already_our_space(tmp_path):
    """Environment findings §2. A pixel round trip here would round twice for
    nothing, which is the class of bug AGENTS.md opens with."""
    root = write_bundle(tmp_path / "t.cap", [move(START_TIME * 1000, 0.0805, 0.5347)], [])
    track = to_focus_track(read_take(root), duration=0.2)
    assert track.points[0].x == pytest.approx(0.0805)
    assert track.points[0].y == pytest.approx(0.5347)


def test_a_missing_segment_says_so_rather_than_raising_a_key_error(tmp_path):
    root = write_bundle(tmp_path / "t.cap", [move(1000.0, 0.5, 0.5)], [])
    with pytest.raises(ValueError, match="no segment 3"):
        read_take(root, segment=3)
