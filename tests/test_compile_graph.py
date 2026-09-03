"""The filter graph (architecture.md §6.1) and the ASS captions (§6.2).

Principle 2 from the compiler's side: no model emits an FFmpeg argument, and
everything here is a deterministic projection of a spec a model may have written.
"""

import math
import re

import pytest

from compile.captions import (
    ACTIVE_COLOUR,
    BASE_COLOUR,
    EMPHASIS_COLOUR,
    line_count,
    render_ass,
    timestamp,
    wrap,
)
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
from compile.timeline import EditedCaption, EditedWord, project
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


# --- kinetic captions (§6.2) -------------------------------------------------


def _events(ass: str) -> list[tuple[str, str, str]]:
    """(start, end, text) for every `Dialogue` line, in file order."""
    found = []
    for line in ass.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        head, _, text = line.partition(",,0,0,0,,")
        fields = head.split(",")
        found.append((fields[1], fields[2], text))
    return found


def _lit(text: str, colour: str) -> list[str]:
    """The words wearing one colour, in order."""
    return re.findall(rf"\{{\\1c&H{colour}&\}}(\S+?)\{{\\1c&H{BASE_COLOUR}&\}}", text)


def _plain(text: str) -> str:
    """The event's text with the override tags taken back off."""
    return re.sub(r"\{\\1c&H[0-9A-F]{6}&\}", "", text.split("}", 1)[1])


def _caption(words, t_out=None):
    edited = [EditedWord(t_in=t_in, t_out=t_o, text=w, emphasis=e) for t_in, t_o, w, e in words]
    return EditedCaption(
        t_in=edited[0].t_in, t_out=t_out if t_out is not None else edited[-1].t_out, words=edited
    )


def test_a_plain_profile_emits_one_event_per_caption_block(spec):
    """The renderer §6.2 shipped first, unchanged: the word array is carried and
    not drawn, one timed block per caption."""
    profile = resolve_profile("demo_16x9")
    assert not profile.captions.kinetic
    timeline = project(spec, profile)
    events = _events(render_ass(timeline.captions, profile))
    assert len(events) == len(timeline.captions)
    assert not any(_lit(text, ACTIVE_COLOUR) for _, _, text in events)


def test_a_kinetic_profile_lights_exactly_one_word_at_a_time(spec):
    profile = resolve_profile("shorts_9x16")
    assert profile.captions.kinetic
    timeline = project(spec, profile)
    events = _events(render_ass(timeline.captions, profile))
    assert len(events) > len(timeline.captions), "a block is more than one event now"
    for _, _, text in events:
        assert len(_lit(text, ACTIVE_COLOUR)) == 1


def test_the_lit_word_is_the_one_being_spoken():
    """The whole claim of the mode. Each event begins when its word does."""
    caption = _caption([(0.0, 0.4, "here", False), (0.5, 0.9, "is", False), (1.0, 1.6, "why", False)])
    profile = resolve_profile("shorts_9x16")
    events = _events(render_ass([caption], profile))
    assert [(start, _lit(text, ACTIVE_COLOUR)[0]) for start, _, text in events] == [
        ("0:00:00.00", "here"),
        ("0:00:00.50", "is"),
        ("0:00:01.00", "why"),
    ]


def test_a_word_stays_lit_until_the_next_one_begins():
    """Not until it *ends*. The gaps between spoken words are tens of
    milliseconds and going dark across each one strobes — so the events tile the
    block with no unlit hole, and the last word holds whatever `_hold_minimum`
    added to a block a cut left too short to read."""
    caption = _caption([(0.0, 0.4, "here", False), (0.5, 0.9, "is", False)], t_out=3.0)
    events = _events(render_ass([caption], resolve_profile("shorts_9x16")))
    assert [e[0] for e in events] == ["0:00:00.00", "0:00:00.50"]
    assert [e[1] for e in events] == ["0:00:00.50", "0:00:03.00"]
    for before, after in zip(events, events[1:]):
        assert before[1] == after[0], "the block has no gap between its words"


def test_both_renderers_break_the_same_lines_in_the_same_places(spec):
    """One wrap, read two ways. A kinetic event with its colours stripped is
    character for character the plain event — which is what keeps §9.1's line
    checks measuring the file that was written."""
    plain = resolve_profile("demo_16x9")
    kinetic = plain.model_copy(
        update={"captions": plain.captions.model_copy(update={"kinetic": True})}
    )
    timeline = project(spec, plain)
    was = {text for _, _, text in _events(render_ass(timeline.captions, plain))}
    now = {_plain(text) for _, _, text in _events(render_ass(timeline.captions, kinetic))}
    assert now == {_plain(text) for text in was}


def test_a_window_too_brief_to_print_is_dropped_rather_than_emitted_empty():
    """ASS times are centiseconds, so two words 4ms apart are a real window in
    Python and the same instant in the file. libass given `start == end` draws a
    flicker or nothing; the neighbours meet at that timestamp instead."""
    # 0.301 and 0.304 both print as `.30`, so the window between them has no
    # time in the file however real it is in the projection.
    caption = _caption(
        [(0.0, 0.30, "one", False), (0.301, 0.303, "two", False), (0.304, 0.9, "three", False)]
    )
    events = _events(render_ass([caption], resolve_profile("shorts_9x16")))
    assert all(start != end for start, end, _ in events), "no zero-length event"
    assert [_lit(text, ACTIVE_COLOUR)[0] for _, _, text in events] == ["one", "three"], (
        "the word that could not be printed is skipped, not shown empty"
    )
    for before, after in zip(events, events[1:]):
        assert before[1] == after[0], "dropping a window leaves no hole"


def test_an_emphasized_word_is_coloured_in_both_renderers():
    """`Word.emphasis` has been written since phase 9 and drawn by nothing. It is
    the one model-written field in the caption subtree (§7.1), and a model stage
    whose output no pixel depends on is a stage nobody can review."""
    caption = _caption([(0.0, 0.4, "never", True), (0.5, 0.9, "again", False)], t_out=2.0)
    for name in ("shorts_9x16", "demo_16x9"):
        ass = render_ass([caption], resolve_profile(name))
        assert any(_lit(text, EMPHASIS_COLOUR) == ["never"] for _, _, text in _events(ass)), name


def test_the_lit_word_outranks_emphasis_while_it_is_lit():
    """A word that is both is the one being read right now. Letting emphasis win
    would make it the only word in the block that never lights up."""
    caption = _caption([(0.0, 0.4, "never", True), (0.5, 0.9, "again", False)], t_out=2.0)
    first, second = _events(render_ass([caption], resolve_profile("shorts_9x16")))
    assert _lit(first[2], ACTIVE_COLOUR) == ["never"] and not _lit(first[2], EMPHASIS_COLOUR)
    assert _lit(second[2], ACTIVE_COLOUR) == ["again"] and _lit(second[2], EMPHASIS_COLOUR) == ["never"]


def test_the_style_and_the_tag_that_ends_a_colour_name_one_colour():
    """Written twice in the file and once in the source. A reset disagreeing with
    the style leaves every word after a highlight a shade off, on the profiles
    that highlight and nowhere else."""
    profile = resolve_profile("shorts_9x16")
    ass = render_ass([_caption([(0.0, 0.4, "here", False)])], profile)
    assert f"&H00{BASE_COLOUR}," in ass, "the style's PrimaryColour"
    assert f"{{\\1c&H{BASE_COLOUR}&}}" in ass, "the tag that ends a coloured run"


def test_the_line_count_check_counts_the_lines_the_renderer_wrote(spec):
    """§9.1 reads `line_count` and the renderer reads `wrap_indices`. They are the
    same call now, so a caption cannot pass the check and render over its box."""
    for name in ("shorts_9x16", "demo_16x9"):
        profile = resolve_profile(name)
        for caption in project(spec, profile).captions:
            drawn = _events(render_ass([caption], profile))[0][2]
            assert line_count(caption, profile) == _plain(drawn).count("\\N") + 1
