"""The §4.4 invariant: an impossible edit must be unrepresentable, not merely wrong.

These are half of risk R5's mitigation. They are cheap, and they catch a whole
class of model error before anything renders.
"""

import pytest
from pydantic import ValidationError

from spec import EditDecisions, EditSpec, Removal, Segment, Source, Tier, choose_threshold


def source(duration: float = 10.0) -> Source:
    return Source(source_id="s", path="source/take.mp4", duration=duration, width=1920, height=1080, fps=30.0)


def segment(t_in, t_out, tier=Tier.ESSENTIAL, reason="because"):
    return Segment(t_in=t_in, t_out=t_out, tier=tier, reason=reason)


def removal(t_in, t_out, kind="silence"):
    return Removal(t_in=t_in, t_out=t_out, kind=kind)


def test_a_partition_validates():
    decisions = EditDecisions(removals=[removal(4, 5)], segments=[segment(0, 4), segment(5, 10)])
    assert decisions.covers(10.0)
    assert decisions.removed_duration() == 1.0


@pytest.mark.parametrize(
    "kwargs, message",
    [
        (dict(segments=[segment(0, 4), segment(3, 8)]), "overlapping"),
        (dict(segments=[segment(0, 4), segment(5, 8)]), "gap"),
        (dict(segments=[segment(2, 4)]), "must start at source time 0.0"),
        (dict(removals=[removal(0, 4)], segments=[segment(2, 6)]), "overlapping"),
    ],
)
def test_impossible_partitions_are_rejected(kwargs, message):
    with pytest.raises(ValidationError, match=message):
        EditDecisions(**kwargs)


@pytest.mark.parametrize("t_in, t_out", [(4.0, 2.0), (3.0, 3.0)])
def test_inverted_and_empty_spans_are_rejected(t_in, t_out):
    with pytest.raises(ValidationError):
        segment(t_in, t_out)
    with pytest.raises(ValidationError):
        removal(t_in, t_out)


def test_a_segment_needs_a_reason():
    with pytest.raises(ValidationError):
        Segment(t_in=0, t_out=1, tier=Tier.ESSENTIAL, reason="")


def test_totality_is_checked_against_the_source_duration():
    """`EditDecisions` proves the partition is gapless; only `EditSpec` knows where it must end."""
    partial = EditDecisions(segments=[segment(0, 8)])
    with pytest.raises(ValidationError, match="cover the source only to 8.0s"):
        EditSpec(job_id="j", source=source(10.0), edit=partial)

    EditSpec(job_id="j", source=source(8.0), edit=partial)  # same decisions, shorter source


def test_an_empty_edit_is_the_pre_plan_edit_state():
    spec = EditSpec(job_id="j", source=source())
    assert spec.edit.segments == [] and spec.edit.removals == []


def test_decisions_may_not_run_past_the_source():
    with pytest.raises(ValidationError, match="past the source end"):
        EditSpec(job_id="j", source=source(10.0), edit=EditDecisions(segments=[segment(0, 12)]))


# --- the §4.4.1 projection ---------------------------------------------------


def tiered() -> EditDecisions:
    return EditDecisions(
        segments=[
            segment(0, 4, Tier.ESSENTIAL),
            segment(4, 7, Tier.SUPPORTING),
            segment(7, 10, Tier.OPTIONAL),
        ]
    )


@pytest.mark.parametrize(
    "budget, threshold, duration",
    [
        (10.0, Tier.OPTIONAL, 10.0),
        (9.0, Tier.SUPPORTING, 7.0),
        (5.0, Tier.ESSENTIAL, 4.0),
    ],
)
def test_the_loosest_tier_that_fits_the_budget_wins(budget, threshold, duration):
    assert choose_threshold(tiered(), budget) == (threshold, duration)


def test_an_unsatisfiable_budget_reports_the_overrun_rather_than_failing():
    """`essential` alone overrunning is the expected way a profile fails (§4.4.1)."""
    threshold, duration = choose_threshold(tiered(), budget=2.0)
    assert threshold is Tier.ESSENTIAL
    assert duration == 4.0  # over budget, with a number attached


def test_tiers_are_ordered_not_merely_named():
    assert Tier.ESSENTIAL.at_or_above(Tier.OPTIONAL)
    assert not Tier.OPTIONAL.at_or_above(Tier.SUPPORTING)


def test_an_unedited_spec_expects_its_whole_transcript():
    """§9.2 diffs rendered audio against this. Before `plan_edit` runs, nothing has
    been cut, so the expected transcript is everything — not nothing."""
    from spec import CaptionBlock, Word

    spec = EditSpec(
        job_id="j",
        source=source(10.0),
        captions=[CaptionBlock(t_in=0, t_out=2, words=[Word(t_in=0, t_out=1, text="hello")])],
    )
    assert spec.transcript_after_edit(Tier.ESSENTIAL) == "hello"
