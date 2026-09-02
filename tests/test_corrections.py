"""The correction layer (phase 7, architecture.md §8).

A correction is a person's decision, so the two ways of losing one are what these
tests are about: overwriting it with a cached planner's answer, and dropping it
quietly when the plan moved underneath it. Everything else here is the arithmetic
that keeps §4.4's totality true after a reviewer has had at it — where an
invariant is arithmetic, arithmetic holds it, and the reviewer decides only what
arithmetic cannot.
"""

import json

import pytest
from pydantic import ValidationError

from prefs import resolve_profile
from spec import EditDecisions, EditSpec, Removal, Segment, Source, Tier
from spec.corrections import (
    REINSTATED_REASON,
    Corrections,
    ReinstatedRemoval,
    RetieredSegment,
    StaleCorrection,
    diff_specs,
)


def source(duration: float = 10.0) -> Source:
    return Source(source_id="s", path="source/take.mp4", duration=duration, width=1920, height=1080, fps=30.0)


def spec_with(decisions: EditDecisions, duration: float = 10.0) -> EditSpec:
    return EditSpec(job_id="j", source=source(duration), edit=decisions)


def cut(t_in, t_out, kind="silence") -> Removal:
    return Removal(t_in=t_in, t_out=t_out, kind=kind)


def keep(t_in, t_out, tier=Tier.ESSENTIAL, reason="the claim") -> Segment:
    return Segment(t_in=t_in, t_out=t_out, tier=tier, reason=reason)


PLAN = EditDecisions(
    removals=[cut(4.0, 5.0), cut(8.0, 9.0, "filler")],
    segments=[keep(0.0, 4.0), keep(5.0, 8.0, Tier.SUPPORTING), keep(9.0, 10.0, Tier.OPTIONAL)],
)


def test_no_corrections_leave_the_spec_exactly_as_the_planners_left_it():
    spec = spec_with(PLAN)
    assert Corrections().apply_to(spec) == spec


def test_reinstating_a_removal_returns_the_span_as_a_segment_and_keeps_the_partition_total():
    corrections = Corrections(reinstated=[ReinstatedRemoval(t_in=4.0, t_out=5.0)])
    edit = corrections.apply_to(spec_with(PLAN)).edit

    assert [(r.t_in, r.t_out) for r in edit.removals] == [(8.0, 9.0)]
    assert edit.covers(10.0), "removals and segments must still partition the source (§4.4)"
    returned = next(s for s in edit.segments if s.t_in == 4.0)
    assert returned.t_out == 5.0
    assert returned.reason == REINSTATED_REASON


def test_a_reinstated_span_is_its_own_segment_rather_than_swallowed_by_a_neighbour():
    """Merging would lose one of the two arguments the neighbours were making."""
    edit = Corrections(reinstated=[ReinstatedRemoval(t_in=4.0, t_out=5.0)]).apply_to(spec_with(PLAN)).edit
    assert [(s.t_in, s.t_out) for s in edit.segments] == [(0.0, 4.0), (4.0, 5.0), (5.0, 8.0), (9.0, 10.0)]
    assert next(s for s in edit.segments if s.t_in == 5.0).reason == "the claim"


def test_a_reinstated_span_takes_the_higher_tier_of_what_it_touches():
    """A reviewer who puts a passage back and watches a tight budget drop it again
    has been told the correction worked when it did not."""
    plan = EditDecisions(
        removals=[cut(4.0, 5.0)],
        segments=[keep(0.0, 4.0, Tier.OPTIONAL), keep(5.0, 10.0, Tier.SUPPORTING)],
    )
    edit = Corrections(reinstated=[ReinstatedRemoval(t_in=4.0, t_out=5.0)]).apply_to(spec_with(plan)).edit
    assert next(s for s in edit.segments if s.t_in == 4.0).tier is Tier.SUPPORTING


def test_a_reinstated_span_touching_no_segment_is_essential():
    plan = EditDecisions(removals=[cut(0.0, 4.0), cut(4.0, 6.0)], segments=[keep(6.0, 10.0, Tier.OPTIONAL)])
    edit = Corrections(reinstated=[ReinstatedRemoval(t_in=0.0, t_out=4.0)]).apply_to(spec_with(plan)).edit
    assert next(s for s in edit.segments if s.t_in == 0.0).tier is Tier.ESSENTIAL


def test_re_tiering_moves_one_segment_and_leaves_its_reason_alone():
    """The reason is `plan_edit`'s argument for the passage. Overruling the tier
    is not the same as claiming the argument was never made."""
    corrections = Corrections(retiered=[RetieredSegment(t_in=5.0, tier=Tier.ESSENTIAL)])
    edit = corrections.apply_to(spec_with(PLAN)).edit
    moved = next(s for s in edit.segments if s.t_in == 5.0)
    assert moved.tier is Tier.ESSENTIAL
    assert moved.reason == "the claim"
    assert [s.tier for s in edit.segments if s.t_in != 5.0] == [Tier.ESSENTIAL, Tier.OPTIONAL]


def test_a_budget_correction_moves_the_budget_and_nothing_else():
    profile = resolve_profile("shorts_9x16")
    corrected = Corrections(budgets={"shorts_9x16": 9.0}).apply_to_profile(profile)
    assert corrected.duration_budget == 9.0
    assert corrected.model_dump(exclude={"duration_budget"}) == profile.model_dump(
        exclude={"duration_budget"}
    )


def test_a_budget_for_another_profile_leaves_this_one_untouched():
    """Per-profile is the point: §4.1 has two layers so a shorter short does not
    shorten the demo."""
    profile = resolve_profile("demo_16x9")
    assert Corrections(budgets={"shorts_9x16": 9.0}).apply_to_profile(profile) == profile


@pytest.mark.parametrize(
    "corrections, message",
    [
        (Corrections(reinstated=[ReinstatedRemoval(t_in=1.0, t_out=2.0)]), "not in the current plan"),
        (Corrections(retiered=[RetieredSegment(t_in=3.5, tier=Tier.OPTIONAL)]), "not in the current plan"),
    ],
)
def test_a_correction_addressing_something_the_plan_no_longer_has_is_refused(corrections, message):
    """Skipping it would be the same failure as overwriting it: the loop appears
    to work and the video does not change."""
    with pytest.raises(StaleCorrection, match=message):
        corrections.apply_to(spec_with(PLAN))


def test_two_corrections_for_the_same_element_are_rejected_at_the_schema():
    with pytest.raises(ValidationError, match="two corrections address"):
        Corrections(
            retiered=[
                RetieredSegment(t_in=5.0, tier=Tier.OPTIONAL),
                RetieredSegment(t_in=5.0, tier=Tier.ESSENTIAL),
            ]
        )


def test_corrections_survive_a_round_trip_through_the_job_directory(tmp_path):
    corrections = Corrections(
        reinstated=[ReinstatedRemoval(t_in=4.0, t_out=5.0)],
        retiered=[RetieredSegment(t_in=9.0, tier=Tier.ESSENTIAL)],
        budgets={"shorts_9x16": 12.0},
    )
    corrections.write(tmp_path)
    assert Corrections.load(tmp_path) == corrections
    assert json.loads((tmp_path / "corrections.json").read_text())["budgets"] == {"shorts_9x16": 12.0}


def test_a_directory_with_no_corrections_reads_as_no_corrections(tmp_path):
    assert Corrections.load(tmp_path).empty


# --- the diff, which is what §10 learns from ---------------------------------


def test_the_diff_names_the_removal_that_was_put_back_and_does_not_double_count_its_segment():
    proposed = spec_with(PLAN)
    corrections = Corrections(reinstated=[ReinstatedRemoval(t_in=4.0, t_out=5.0)])
    diff = diff_specs(proposed, corrections.apply_to(proposed))

    assert [c.path for c in diff.changes] == ["edit.removals[4.000-5.000]"]
    assert diff.changes[0].before == "silence"
    assert diff.changes[0].after is None


def test_the_diff_names_the_tier_that_moved_with_both_ends_of_the_move():
    proposed = spec_with(PLAN)
    corrections = Corrections(retiered=[RetieredSegment(t_in=5.0, tier=Tier.ESSENTIAL)])
    diff = diff_specs(proposed, corrections.apply_to(proposed))

    assert [(c.path, c.before, c.after) for c in diff.changes] == [
        ("edit.segments[5.000].tier", "supporting", "essential")
    ]


def test_the_diff_carries_the_budget_as_a_number_because_the_learner_takes_its_median():
    profile = resolve_profile("shorts_9x16")
    corrected = Corrections(budgets={"shorts_9x16": 9.0}).apply_to_profile(profile)
    spec = spec_with(PLAN)
    diff = diff_specs(spec, spec, [profile], [corrected])

    change = diff.changes[0]
    assert change.path == "profiles.shorts_9x16.duration_budget"
    assert (change.before, change.after) == (profile.duration_budget, 9.0)


def test_a_correction_that_changes_nothing_is_not_a_change():
    """The diff is derived from the documents rather than from the corrections
    that produced them, so it cannot claim an edit the render did not get."""
    profile = resolve_profile("shorts_9x16")
    corrections = Corrections(
        retiered=[RetieredSegment(t_in=0.0, tier=Tier.ESSENTIAL)],
        budgets={"shorts_9x16": profile.duration_budget},
    )
    spec = spec_with(PLAN)
    diff = diff_specs(
        spec, corrections.apply_to(spec), [profile], [corrections.apply_to_profile(profile)]
    )
    assert diff.empty
