"""`plan_focus` (architecture.md §4.3).

No model participates in the highest-impact spatial decision in the pipeline, so
these are the tests that stand in for one.
"""

import math

import pytest

from ingest.fixtures import DEFAULT_BEATS, build_spec
from plan import plan_focus
from plan.focus import CropPathPlan, ZoomPlan, window_size
from prefs import resolve_profile
from spec import FocusPoint, FocusTrack


@pytest.fixture(scope="module")
def spec():
    return build_spec().spec


@pytest.fixture(scope="module")
def shorts():
    return resolve_profile("shorts_9x16")


@pytest.fixture(scope="module")
def demo():
    return resolve_profile("demo_16x9")


def test_the_crop_window_is_the_largest_one_of_the_output_aspect(spec, shorts):
    w, h = window_size(spec, shorts)
    assert h == 1.0, "a 9:16 window out of 16:9 should use the full source height"
    assert math.isclose(w * spec.source.width / (h * spec.source.height), shorts.width / shorts.height, rel_tol=1e-6)


def test_the_crop_window_never_changes_size(spec, shorts):
    """A window that also changed size would make §9.1's judder check answer two
    questions at once, and would re-scale every frame on a machine that cannot
    afford it (§16)."""
    plan = plan_focus(spec, shorts)
    assert isinstance(plan, CropPathPlan)
    assert plan.window_w > 0 and plan.window_h == 1.0


def test_the_crop_path_respects_the_judder_ceiling(spec, shorts):
    """Judder is *the* failure mode of automated vertical reframing, and it is
    invisible in a still frame."""
    plan = plan_focus(spec, shorts)
    assert plan.max_step() <= shorts.focus.max_crop_delta_per_frame + 1e-9


def test_the_crop_path_stays_inside_the_frame(spec, shorts):
    plan = plan_focus(spec, shorts)
    half_w, half_h = plan.window_w / 2, plan.window_h / 2
    for sample in plan.samples:
        assert half_w - 1e-9 <= sample.cx <= 1.0 - half_w + 1e-9
        assert half_h - 1e-9 <= sample.cy <= 1.0 - half_h + 1e-9


def test_the_crop_trails_the_cursor_rather_than_snapping_to_it(spec, shorts):
    """The lag is deliberate and learnable; a crop that snaps reads as nervous."""
    plan = plan_focus(spec, shorts)
    jump = spec.focus.points[0]
    assert abs(plan.samples[1].cx - jump.x) > 1e-6 or abs(plan.samples[1].cy - jump.y) > 1e-6


def test_zoom_regions_land_on_the_click_clusters(spec, demo):
    plan = plan_focus(spec, demo)
    assert isinstance(plan, ZoomPlan)
    assert len(plan.regions) == len(DEFAULT_BEATS), "one region per beat the cursor rested on"
    for beat, region in zip(DEFAULT_BEATS, plan.regions):
        # Clamped so the magnified window stays in frame, so the centre moves
        # toward the middle — but never past it, and never to the wrong side.
        assert (region.cx - 0.5) * (beat.target[0] - 0.5) >= 0
        assert (region.cy - 0.5) * (beat.target[1] - 0.5) >= 0


def test_zoom_regions_do_not_overlap(spec, demo):
    plan = plan_focus(spec, demo)
    for a, b in zip(plan.regions, plan.regions[1:]):
        assert b.t_in >= a.t_out, "overlapping ramps would sum above the intended zoom"


def test_a_transient_pass_is_not_a_dwell(spec, demo):
    """`min_dwell_ms` exists so the frame does not chase the cursor across the screen."""
    quick = spec.model_copy(
        update={
            "focus": FocusTrack(
                points=[
                    FocusPoint(t=0.0, x=0.2, y=0.2, kind="click"),
                    FocusPoint(t=0.05, x=0.8, y=0.8, kind="click"),
                ]
            )
        }
    )
    assert plan_focus(quick, demo).regions == []


def test_an_empty_track_frames_the_centre(spec, shorts):
    """The still path: no cursor, so no reason to look anywhere in particular."""
    empty = spec.model_copy(update={"focus": FocusTrack(points=[])})
    plan = plan_focus(empty, shorts)
    assert all(s.cx == pytest.approx(0.5) for s in plan.samples)
