"""The filter graph (architecture.md §6.1) and the ASS captions (§6.2).

Principle 2 from the compiler's side: no model emits an FFmpeg argument, and
everything here is a deterministic projection of a spec a model may have written.
"""

import math

import pytest

from compile.captions import render_ass, timestamp, wrap
from compile.graph import (
    OutputZoomRegion,
    _zoom_at,
    build_commands,
    build_graph,
    clamp_to_safe_area,
    encode_args,
    output_zoom_regions,
    view_rects,
    zoom_expressions,
)
from compile.overlays import OverlayAsset, render_asset
from compile.timeline import project
from ingest.fixtures import build_spec
from plan import plan_focus
from prefs import resolve_profile
from spec import Encoder


@pytest.fixture(scope="module")
def spec():
    return build_spec().spec


def _pieces(spec, profile_name, tmp_path):
    profile = resolve_profile(profile_name)
    timeline = project(spec, profile)
    focus = plan_focus(spec, profile)
    assets = [
        render_asset(o.template, o.text, profile, tmp_path, i) for i, o in enumerate(timeline.overlays)
    ]
    return profile, timeline, focus, assets


def _graph(spec, profile_name, tmp_path):
    profile, timeline, focus, assets = _pieces(spec, profile_name, tmp_path)
    return build_graph(
        spec, profile, timeline, focus, assets,
        ass_name="captions.ass", commands_name="commands.txt",
        audio_commands_name="audio.txt", music_input=None,
    )


def test_the_graph_cuts_before_it_transforms(spec, tmp_path):
    """Trimming first means the expensive scale runs only on frames that survive —
    most of the render time on the target machine (§16)."""
    graph = _graph(spec, "shorts_9x16", tmp_path)
    assert graph.index("trim=start=") < graph.index("scale=")
    assert graph.count("[0:v]trim=start=") == 4, "one per surviving span"
    assert "concat=n=4:v=1:a=1[vc][ac]" in graph


def test_video_and_audio_are_cut_at_the_same_instants(spec, tmp_path):
    """Removal is a range, not rewritten text: the audio and the caption are cut at
    the same instants, from the same decision."""
    graph = _graph(spec, "shorts_9x16", tmp_path)
    video = sorted(part.split("trim=")[1].split(",")[0] for part in graph.split(";") if "[0:v]trim" in part)
    audio = sorted(part.split("atrim=")[1].split(",")[0] for part in graph.split(";") if "[0:a]atrim" in part)
    assert video == audio


def test_crop_mode_moves_a_constant_window_by_command(spec, tmp_path):
    graph = _graph(spec, "shorts_9x16", tmp_path)
    assert "sendcmd=f=commands.txt" in graph
    assert "crop@focus=w=608:h=1080" in graph, "608x1080 is 9:16 out of a 1920x1080 source"
    assert "zoompan" not in graph


def test_zoom_mode_uses_an_expression_because_zoompan_takes_no_commands(spec, tmp_path):
    graph = _graph(spec, "demo_16x9", tmp_path)
    assert "zoompan=z='1+" in graph
    assert "crop@focus" not in graph


def test_captions_are_burned_last_so_nothing_sits_on_them(spec, tmp_path):
    graph = _graph(spec, "shorts_9x16", tmp_path)
    assert graph.index("overlay@o") < graph.index("ass=f=")
    assert graph.rstrip().endswith("[aout]")


def test_the_expression_and_the_python_evaluator_agree(spec, tmp_path):
    """The trapezoid exists twice — once as an FFmpeg expression the render uses,
    once in Python for the overlay projection and the tests. Two implementations of
    one formula is exactly the pair that silently drifts, so check them against
    each other rather than trusting the comment that says they match."""
    profile, timeline, focus, _ = _pieces(spec, "demo_16x9", tmp_path)
    regions = output_zoom_regions(focus, timeline)
    zoom_expr, _, _ = zoom_expressions(regions, focus.ease)
    namespace = {"clip": lambda v, lo, hi: min(max(v, lo), hi), "min": min, "max": max}
    for step in range(0, int(timeline.duration * 4)):
        t = step / 4
        expected = _zoom_at(regions, focus.ease, t)[0]
        actual = eval(zoom_expr, {"__builtins__": {}}, {**namespace, "in_time": t})
        assert actual == pytest.approx(expected, abs=1e-9), f"at t={t}"


def test_no_zoom_regions_means_no_zoom(spec, tmp_path):
    assert zoom_expressions([], 0.35) == ("1", "0", "0")


def test_a_zoom_region_cut_in_half_survives_as_the_half_that_is_left(spec, tmp_path):
    _, timeline, focus, _ = _pieces(spec, "demo_16x9", tmp_path)
    regions = output_zoom_regions(focus, timeline)
    assert regions, "the fixture's dwells must survive into the output"
    for region in regions:
        assert 0.0 <= region.t_in < region.t_out <= timeline.duration + 1e-9


def test_commands_are_emitted_only_when_a_value_changes(spec, tmp_path):
    """Readability, not size: a wrong path should be something you can see."""
    profile, timeline, focus, assets = _pieces(spec, "shorts_9x16", tmp_path)
    rects = view_rects(timeline, focus, profile)
    commands = build_commands(timeline, rects, profile, spec, dict(enumerate(assets)))
    lines = commands.strip().splitlines()
    assert lines, "a moving crop must produce commands"
    assert len(lines) < len(rects) * 5
    for line in lines:
        assert line.endswith(";") and line.count(" ") >= 3


def test_the_view_rect_never_leaves_the_source_frame(spec, tmp_path):
    for name in ("shorts_9x16", "demo_16x9"):
        profile, timeline, focus, _ = _pieces(spec, name, tmp_path)
        for rect in view_rects(timeline, focus, profile):
            assert rect.x >= -1e-9 and rect.y >= -1e-9
            assert rect.x + rect.w <= 1.0 + 1e-9 and rect.y + rect.h <= 1.0 + 1e-9


def test_an_overlay_that_follows_a_point_off_frame_is_clamped_into_the_safe_area(spec, tmp_path):
    profile = resolve_profile("shorts_9x16")
    asset = OverlayAsset(template="label_chip", path=tmp_path / "x.png", width=200, height=80, dx=0, dy=0)
    x, y = clamp_to_safe_area(-5000, 9000, asset, profile)
    assert x >= int(profile.safe_area.left * profile.width)
    assert y + asset.height <= int((1.0 - profile.safe_area.bottom) * profile.height) + 1


def test_the_two_encoders_use_their_own_quality_scales(spec):
    profile = resolve_profile("demo_16x9")
    software = encode_args(profile, Encoder.SOFTWARE)
    hardware = encode_args(profile, Encoder.VIDEOTOOLBOX)
    assert "libx264" in software and "-crf" in software
    assert "+bitexact" in software, "golden renders must be reproducible (§11, §16)"
    assert "h264_videotoolbox" in hardware and "-q:v" in hardware
    assert "-crf" not in hardware, "crf steers x264 and nothing else"


# --- captions ---------------------------------------------------------------


def test_the_ass_file_is_sized_to_the_profile(spec):
    profile = resolve_profile("shorts_9x16")
    timeline = project(spec, profile)
    ass = render_ass(timeline.captions, profile)
    assert f"PlayResX: {profile.width}" in ass and f"PlayResY: {profile.height}" in ass
    assert f",{int(round(profile.captions.type_scale * profile.height))}," in ass
    assert ass.count("Dialogue:") == len(timeline.captions)


def test_caption_events_are_in_output_time(spec):
    profile = resolve_profile("shorts_9x16")
    timeline = project(spec, profile)
    ass = render_ass(timeline.captions, profile)
    assert "Dialogue: 0,0:00:00.00," in ass, "the first caption starts at the start of the render"


@pytest.mark.parametrize(
    "seconds, expected",
    [(0.0, "0:00:00.00"), (4.32, "0:00:04.32"), (59.999, "0:01:00.00"), (3661.5, "1:01:01.50")],
)
def test_timestamps_carry_rather_than_printing_a_hundredth_hundredth(seconds, expected):
    assert timestamp(seconds) == expected


def test_wrapping_is_greedy_and_predictable():
    assert wrap("the export button is the part people miss", 20) == "the export button is\\Nthe part people miss"
    assert wrap("", 20) == ""
    assert wrap("supercalifragilistic", 5) == "supercalifragilistic", "a long word is not broken"


def test_a_profiles_captions_fit_its_box():
    """The profile validator, seen from the compiler's side."""
    for name in ("shorts_9x16", "demo_16x9"):
        profile = resolve_profile(name)
        longest = "x" * profile.captions.max_chars_per_line
        assert len(wrap(longest, profile.captions.max_chars_per_line).split("\\N")) == 1
