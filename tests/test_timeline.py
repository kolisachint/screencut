"""The time projection (architecture.md §4.5).

`EditDecisions` is never applied to the spec. These tests are what says so: the
spec that goes in is the spec that comes out, and every cut lives in the thing the
compiler produces.
"""

import pytest

from compile.timeline import MIN_OVERLAY_PIECE_S, project
from ingest.fixtures import build_spec
from prefs import resolve_profile
from spec import (
    CaptionBlock,
    EditDecisions,
    EditSpec,
    OverlayIntent,
    Point,
    Removal,
    Segment,
    Source,
    Tier,
    Word,
)


@pytest.fixture(scope="module")
def spec():
    return build_spec().spec


@pytest.fixture(scope="module")
def shorts():
    return resolve_profile("shorts_9x16")


@pytest.fixture(scope="module")
def demo():
    return resolve_profile("demo_16x9")


def test_projecting_does_not_touch_the_spec(spec, shorts):
    """Decision #22 in one assertion."""
    before = spec.model_dump_json()
    project(spec, shorts)
    assert spec.model_dump_json() == before


def test_one_spec_two_profiles_two_lengths(spec, shorts, demo):
    short = project(spec, shorts)
    long = project(spec, demo)
    assert short.duration < long.duration
    assert short.threshold.rank > long.threshold.rank
    assert short.duration <= shorts.duration_budget


def test_the_same_profile_at_two_budgets_gives_two_lengths(spec, shorts):
    """The budget is one scalar, and it is what "make the short shorter" edits (§8)."""
    tight = project(spec, shorts.model_copy(update={"duration_budget": 9.0}))
    loose = project(spec, shorts.model_copy(update={"duration_budget": 60.0}))
    assert tight.duration < loose.duration
    assert tight.threshold is Tier.ESSENTIAL


def test_removed_time_does_not_reach_the_output(spec, shorts):
    timeline = project(spec, shorts)
    assert timeline.duration == pytest.approx(sum(s.duration for s in timeline.spans))
    for span in timeline.spans:
        midpoint = (span.source_in + span.source_out) / 2
        assert not spec.edit.is_removed(midpoint)


def test_time_maps_both_ways(spec, shorts):
    timeline = project(spec, shorts)
    for span in timeline.spans:
        t = span.source_in + span.duration / 2
        output = timeline.output_at(t)
        assert output is not None
        assert timeline.source_at(output) == pytest.approx(t)


def test_removed_source_time_maps_to_nothing(spec, shorts):
    timeline = project(spec, shorts)
    removal = spec.edit.removals[0]
    assert timeline.output_at((removal.t_in + removal.t_out) / 2) is None


def test_a_cut_inside_a_block_splits_it_and_clips_no_word(spec, shorts):
    """The filler removal lands mid-block. Both halves survive as blocks, and the
    word that was cut appears in neither — §6.2's word timings making §4.5 exact."""
    timeline = project(spec, shorts)
    words = [w.text for caption in timeline.captions for w in caption.words]
    assert "um" not in words
    assert "so" in words and "clicking" in words
    texts = [c.text for c in timeline.captions]
    assert "so" in texts, "the fragment before the cut is its own block"


def test_captions_never_overlap_after_splitting(spec, shorts):
    timeline = project(spec, shorts)
    for a, b in zip(timeline.captions, timeline.captions[1:]):
        assert a.t_out <= b.t_in + 1e-9


def test_captions_stay_inside_the_render(spec, shorts):
    timeline = project(spec, shorts)
    for caption in timeline.captions:
        assert 0.0 <= caption.t_in < caption.t_out <= timeline.duration + 1e-9


def test_an_overlay_inside_a_removal_is_dropped(spec, shorts):
    """`plan_overlays` runs against the full timeline and may spend an overlay on
    footage that is later cut. Compile drops it, deterministically."""
    timeline = project(spec, shorts)
    assert timeline.dropped_overlays == 1
    assert all(o.template != "callout_arrow" for o in timeline.overlays)


def test_a_sliver_left_by_a_cut_is_dropped_rather_than_flashed(spec, shorts):
    timeline = project(spec, shorts)
    for overlay in timeline.overlays:
        assert overlay.t_out - overlay.t_in >= MIN_OVERLAY_PIECE_S or overlay.spans_whole_output


def test_a_whole_output_overlay_spans_the_render(spec, shorts):
    """No second time base: it needs no anchor, so compile gives it the duration
    it produced (§4.5)."""
    timeline = project(spec, shorts)
    pill = next(o for o in timeline.overlays if o.spans_whole_output)
    assert (pill.t_in, pill.t_out) == (0.0, timeline.duration)
    assert pill.anchor is None


def test_adjacent_surviving_segments_do_not_become_a_cut(shorts):
    """Two tiers that both survive are one continuous stretch of footage."""
    spec = _spec(
        segments=[
            Segment(t_in=0, t_out=4, tier=Tier.ESSENTIAL, reason="a"),
            Segment(t_in=4, t_out=8, tier=Tier.ESSENTIAL, reason="b"),
        ],
        duration=8.0,
    )
    timeline = project(spec, shorts)
    assert len(timeline.spans) == 1
    assert timeline.cuts == []


def test_an_unplanned_spec_renders_the_whole_take(shorts):
    """Phase 4 renders here, before `plan_edit` exists. It must not render nothing."""
    spec = _spec(segments=[], duration=10.0)
    timeline = project(spec, shorts)
    assert timeline.duration == 10.0
    assert len(timeline.spans) == 1


def test_an_unsatisfiable_budget_is_reported_with_a_number(shorts):
    """§4.4.1's expected failure: `essential` alone overrunning."""
    spec = _spec(
        segments=[Segment(t_in=0, t_out=40, tier=Tier.ESSENTIAL, reason="all of it")],
        duration=40.0,
    )
    timeline = project(spec, shorts.model_copy(update={"duration_budget": 10.0}))
    assert timeline.budget_overrun == pytest.approx(30.0)
    assert timeline.duration == pytest.approx(40.0), "reported, not silently truncated"


def test_a_cut_block_too_short_to_read_is_held(shorts):
    spec = _spec(
        segments=[Segment(t_in=0, t_out=0.4, tier=Tier.ESSENTIAL, reason="a")],
        removals=[Removal(t_in=0.4, t_out=10.0, kind="silence")],
        duration=10.0,
        captions=[CaptionBlock(t_in=0, t_out=0.4, words=[Word(t_in=0, t_out=0.3, text="hi")])],
    )
    timeline = project(spec, shorts)
    caption = timeline.captions[0]
    assert caption.t_out - caption.t_in >= min(shorts.captions.min_display_s, timeline.duration)


def _spec(*, segments, duration, removals=(), captions=(), overlays=()) -> EditSpec:
    return EditSpec(
        job_id="t",
        source=Source(source_id="s", path="source/a.mp4", duration=duration, width=1920, height=1080, fps=30),
        edit=EditDecisions(removals=list(removals), segments=list(segments)),
        captions=list(captions),
        overlays=list(overlays),
    )
