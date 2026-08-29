"""The synthetic fixture (decision #10) and the ingest adapter boundary (risk R1)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ingest.events import RecorderEvents, to_focus_track
from ingest.fixtures import DEFAULT_BEATS, build_spec, source_ffmpeg_command, write_fixture
from spec import DEMO_16X9, SHORTS_9X16, FocusKind, RemovalKind, Tier, choose_threshold, load_spec_file


@pytest.fixture(scope="module")
def fixture():
    return build_spec()


def test_the_fixture_is_a_valid_total_edit(fixture):
    spec = fixture.spec
    assert spec.edit.covers(spec.source.duration)
    assert spec.source.duration == len(DEFAULT_BEATS) * fixture.slot_s


def test_the_fixture_carries_the_material_later_phases_need(fixture):
    kinds = {r.kind for r in fixture.spec.edit.removals}
    assert kinds == {RemovalKind.SILENCE, RemovalKind.FILLER}, "trim needs both to find in phase 5"
    assert {s.tier for s in fixture.spec.edit.segments} == set(Tier), "all three tiers, so budgets diverge"
    assert any(o.spans_whole_output for o in fixture.spec.overlays)
    assert any(
        not o.spans_whole_output and fixture.spec.edit.is_removed((o.t_in + o.t_out) / 2)
        for o in fixture.spec.overlays
    ), "one overlay must land inside a removal, so compile's drop path is exercised (§4.5)"


def test_a_filler_removal_lines_up_exactly_with_its_word(fixture):
    filler = next(r for r in fixture.spec.edit.removals if r.kind is RemovalKind.FILLER)
    words = [w for block in fixture.spec.captions for w in block.words]
    assert any(w.t_in == filler.t_in and w.t_out == filler.t_out for w in words)
    assert "um" not in fixture.spec.transcript_after_edit(Tier.OPTIONAL).split()


def test_one_spec_renders_at_two_lengths_under_the_two_builtin_profiles(fixture):
    """The §4.4.1 payoff: cuts stay aspect-agnostic and the profile decides how much fits."""
    short_tier, short_len = choose_threshold(fixture.spec.edit, SHORTS_9X16.duration_budget)
    demo_tier, demo_len = choose_threshold(fixture.spec.edit, DEMO_16X9.duration_budget)
    assert short_len < demo_len
    assert short_tier.rank > demo_tier.rank
    assert short_len <= SHORTS_9X16.duration_budget


def test_the_expected_transcript_differs_per_profile(fixture):
    """§9.2 diffs rendered audio against this, not against the raw transcript."""
    short = fixture.spec.transcript_after_edit(choose_threshold(fixture.spec.edit, SHORTS_9X16.duration_budget)[0])
    demo = fixture.spec.transcript_after_edit(choose_threshold(fixture.spec.edit, DEMO_16X9.duration_budget)[0])
    assert short != demo
    assert short in demo or len(short.split()) < len(demo.split())
    assert "um" not in short.split() and "um" not in demo.split()


def test_the_adapter_turns_recorder_units_into_our_format(fixture):
    """`FocusTrack` is our format, not the recorder's (risk R1)."""
    track = to_focus_track(fixture.events)
    assert all(0.0 <= p.x <= 1.0 and 0.0 <= p.y <= 1.0 for p in track.points)
    assert len(track.clicks()) >= len(DEFAULT_BEATS)
    assert any(p.kind is FocusKind.DWELL for p in track.points)
    assert track.points == fixture.spec.focus.points


def test_the_adapter_ignores_fields_a_recorder_invents():
    """Somebody else's format, so extra keys are tolerated — the opposite of the spec."""
    events = RecorderEvents.model_validate(
        {
            "width": 100, "height": 100, "duration": 1.0,
            "samples": [{"t": 0.0, "x": 10, "y": 10, "pressure": 0.4}],
            "clicks": [], "window_bounds": [0, 0, 100, 100],
        }
    )
    assert len(events.samples) == 1


def test_clicks_land_near_the_beat_targets(fixture):
    for beat in DEFAULT_BEATS:
        assert any(
            abs(p.x - beat.target[0]) < 0.05 and abs(p.y - beat.target[1]) < 0.05
            for p in fixture.spec.focus.clicks()
        ), f"no click near {beat.target}"


def test_writing_a_fixture_produces_a_loadable_job_directory(tmp_path, fixture):
    out = write_fixture(tmp_path / "job", fixture, with_video=False)
    reloaded = load_spec_file(out / "spec.json")
    assert reloaded == fixture.spec
    events = RecorderEvents.load(out / "source" / "events.json")
    assert events.duration == fixture.spec.source.duration


def test_the_written_spec_is_stable_across_runs(tmp_path):
    """A fixture that changes bytes between runs cannot be promoted into `golden/`."""
    first = write_fixture(tmp_path / "a", build_spec(), with_video=False) / "spec.json"
    second = write_fixture(tmp_path / "b", build_spec(), with_video=False) / "spec.json"
    assert first.read_text() == second.read_text()


def test_the_source_command_is_bit_exact_and_silent_in_the_gaps(fixture):
    command = source_ffmpeg_command(fixture, Path("out.mp4"))
    assert "+bitexact" in command
    joined = " ".join(command)
    assert "volume=volume=0:enable=" in joined, "the gaps must be real silence for trim to find"
    assert joined.count("drawbox") == len(DEFAULT_BEATS)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_the_generated_source_video_matches_the_spec(tmp_path, fixture):
    out = write_fixture(tmp_path / "job", fixture, with_video=True)
    media = out / fixture.spec.source.path
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-show_entries", "stream=width,height", "-of", "json", str(media)],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(probe.stdout)
    assert abs(float(data["format"]["duration"]) - fixture.spec.source.duration) < 0.1
    video = next(s for s in data["streams"] if "width" in s)
    assert (video["width"], video["height"]) == (fixture.spec.source.width, fixture.spec.source.height)
