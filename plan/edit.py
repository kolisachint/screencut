"""`plan_edit` — the model reviews the arithmetic (architecture.md §7.1, §4.4).

The first model stage, and the one §1.1 rests on. `trim` has already proposed
removals by threshold and lookup; this stage receives that proposal alongside the
transcript and may reject any of it, add false starts and restarts of its own,
and rank what remains into tiers.

**Why the model sees the proposal rather than being kept away from it** (§7.1): a
two-second gap can be dead air or a deliberate beat, and only one of those is
removable. Arithmetic cannot tell them apart and should not be the last word. The
cost is honest — a model that can override a correct trim can also override it
wrongly — and it is bounded by §9.1 and visible in review.

**The model returns intent; the partition is derived.** `EditDecisions` demands a
gapless, non-overlapping cover of exactly `[0, duration]`, and asking a language
model to land that in float arithmetic buys a retry on almost every call for no
editorial gain. So the fragment carries removals and tiered segments as *ranges
the model cares about*, and `reconcile` turns them into the total partition
deterministically: removals win, segments are clipped to what is left, and
anything nobody tiered becomes `essential`. §4.4's totality still holds by
construction — it is simply arithmetic that holds it, which is principle 2 applied
to time exactly as §4.5 applies it to cuts.

**`proposed_by` is derived, not reported.** A removal overlapping one of `trim`'s
proposals came from `trim` whatever the model says about it, and the §9.1 override
rate is a number about the model that the model therefore cannot write itself.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from spec.edit import EditDecisions, Removal, RemovalKind, Segment, Tier
from spec.focus import FocusKind, FocusTrack
from spec.origin import Stage
from spec.types import TIME_EPS

UNTIERED_REASON = "kept by default; plan_edit ranked no segment here"
"""A segment the model left unranked. `essential` is the safe direction — losing
material nobody looked at is worse than running long, and §9.1 reports the
overrun with a number attached either way."""


class ProposedRemoval(BaseModel):
    """One range the model wants gone. No `proposed_by`: that is derived."""

    model_config = ConfigDict(extra="forbid")

    t_in: Annotated[float, Field(ge=0.0, description="Source seconds.")]
    t_out: Annotated[float, Field(ge=0.0, description="Source seconds.")]
    kind: RemovalKind


class ProposedSegment(BaseModel):
    """One range the model wants kept, with the tier it earned and why."""

    model_config = ConfigDict(extra="forbid")

    t_in: Annotated[float, Field(ge=0.0)]
    t_out: Annotated[float, Field(ge=0.0)]
    tier: Tier
    reason: Annotated[str, Field(min_length=1, max_length=200)]


class EditPlan(BaseModel):
    """The §7.2 fragment: what the model returns, and all it returns."""

    model_config = ConfigDict(extra="forbid")

    removals: list[ProposedRemoval] = Field(default_factory=list)
    segments: list[ProposedSegment] = Field(default_factory=list)


INSTRUCTION = """\
You are the editorial stage of a screen-recording pipeline. You are given a
transcript with word timings, a list of removals that arithmetic has already
proposed, and a summary of where the cursor was.

Decide what survives.

- Reject any proposed removal that is a deliberate beat rather than dead air. A
  pause before the point of the video is worth keeping; a pause while somebody
  finds a window is not.
- Add removals of your own for false starts, restarts and sentences that repeat
  what was already said. Use kind "false_start" or "redundant" for these.
- Never cut through a word: put every boundary in a gap between word timings.
- Then rank what is left into segments. "essential" is what the video makes no
  sense without, "supporting" is worth watching, "optional" is what a short can
  drop and lose nothing. Give every segment a reason a person can argue with.
  Rank on merit alone: you are not cutting to a length, and something else
  decides how much of your ranking a given output has room for.

Segments and removals are in source seconds and must not overlap each other.
Cover the parts you care about; anything you leave out is kept as essential.
"""


def focus_summary(track: FocusTrack, *, bucket_s: float = 2.0) -> str:
    """Where attention was, compressed enough to sit in a prompt.

    The whole track is thousands of points and says nothing a model can use. What
    it needs is the one thing `FocusTrack` knows that the transcript does not:
    which stretches were a demonstration and which were a cursor drifting while
    somebody talked. Clicks and dwell are that signal, so those are what is
    summarized and movement is dropped.
    """
    if not track.points:
        return "cursor: no track"
    end = track.points[-1].t
    lines: list[str] = []
    start = 0.0
    while start < end - TIME_EPS:
        stop = min(start + bucket_s, end)
        inside = [p for p in track.points if start <= p.t < stop]
        clicks = sum(1 for p in inside if p.kind is FocusKind.CLICK)
        dwell = sum(1 for p in inside if p.kind is FocusKind.DWELL)
        if clicks or dwell:
            share = dwell / len(inside) if inside else 0.0
            lines.append(f"[{start:.1f}-{stop:.1f}] {clicks} clicks, {share:.0%} dwell")
        start = stop
    return "cursor activity by 2s bucket:\n" + ("\n".join(lines) or "  (no clicks or dwell)")


def build_content(
    words: list,
    proposals: list[Removal],
    track: FocusTrack,
    duration: float,
) -> str:
    """Everything the stage needs, in the prompt.

    With tools off there is no second way to get it there (§7.3), so this is not a
    summary for readability — it is the stage's entire input.

    **No `duration_budget` appears here, and that is the point of §4.4.1.** Tiers
    are aspect-independent taste decided once; which tiers survive is a separate,
    per-profile, arithmetic decision the compiler makes. Putting a budget in the
    prompt would make the ranking depend on the profile — and then one `EditSpec`
    could not render at two lengths, changing a budget would cost a model call,
    and the cache key for the most expensive stage in the pipeline would move
    every time somebody wanted a shorter short.
    """
    lines = [f"Source duration: {duration:.2f}s", ""]
    lines.append("Transcript, [start-end] word:")
    lines.append(
        " ".join(f"[{w.t_in:.2f}-{w.t_out:.2f}] {w.text}" for w in words) or "  (no speech)"
    )
    lines.append("")
    lines.append("Removals already proposed by arithmetic (accept or reject each):")
    for removal in proposals:
        lines.append(f"  [{removal.t_in:.2f}-{removal.t_out:.2f}] {removal.kind.value}")
    if not proposals:
        lines.append("  (none)")
    lines.append("")
    lines.append(focus_summary(track))
    return "\n".join(lines)


def reconcile(plan: EditPlan, proposals: list[Removal], duration: float) -> EditDecisions:
    """The model's intent into a total partition (§4.4).

    Removals win over segments, because a range the model asked to cut is not a
    range it also asked to keep, and resolving that the other way would render
    material somebody decided against.
    """
    removals = _removals(plan, proposals, duration)
    segments = _segments(plan, removals, duration)
    return EditDecisions(removals=removals, segments=segments)


def _removals(plan: EditPlan, proposals: list[Removal], duration: float) -> list[Removal]:
    spans: list[tuple[float, float, RemovalKind]] = []
    for proposed in plan.removals:
        t_in, t_out = max(proposed.t_in, 0.0), min(proposed.t_out, duration)
        if t_out - t_in > TIME_EPS:
            spans.append((t_in, t_out, proposed.kind))
    spans.sort()

    merged: list[list] = []
    for t_in, t_out, kind in spans:
        if merged and t_in <= merged[-1][1] + TIME_EPS:
            merged[-1][1] = max(merged[-1][1], t_out)
        else:
            merged.append([t_in, t_out, kind])

    return [
        Removal(
            t_in=t_in,
            t_out=t_out,
            kind=kind,
            # Derived, so the override rate is a number about the model rather
            # than one the model writes about itself.
            proposed_by=Stage.TRIM if _overlaps_any(t_in, t_out, proposals) else Stage.PLAN_EDIT,
        )
        for t_in, t_out, kind in merged
    ]


def _overlaps_any(t_in: float, t_out: float, proposals: list[Removal]) -> bool:
    return any(t_in < p.t_out - TIME_EPS and p.t_in < t_out - TIME_EPS for p in proposals)


def _segments(plan: EditPlan, removals: list[Removal], duration: float) -> list[Segment]:
    """Everything the removals leave, tiered by whichever proposed segment covers it.

    Split at every boundary the model named, so a gap the model tiered in two
    pieces stays two pieces; then merged again where neighbours agree, so a gap it
    said nothing about does not arrive in review as fifty identical rows.
    """
    gaps = _complement(removals, duration)
    tiered: list[Segment] = []
    for gap_in, gap_out in gaps:
        edges = sorted(
            {gap_in, gap_out}
            | {
                min(max(edge, gap_in), gap_out)
                for proposed in plan.segments
                for edge in (proposed.t_in, proposed.t_out)
            }
        )
        for lower, upper in zip(edges, edges[1:]):
            if upper - lower <= TIME_EPS:
                continue
            covering = _covering(plan, (lower + upper) / 2.0)
            tiered.append(
                Segment(
                    t_in=lower,
                    t_out=upper,
                    tier=covering.tier if covering else Tier.ESSENTIAL,
                    reason=covering.reason if covering else UNTIERED_REASON,
                )
            )
    return _coalesce(tiered)


def _covering(plan: EditPlan, t: float) -> ProposedSegment | None:
    """The model's segment containing `t`. Last one wins if it named two.

    Overlapping proposals are not rejected: the fragment is intent, and a retry
    over two segments that touch would cost a round trip to change nothing."""
    found = None
    for proposed in plan.segments:
        if proposed.t_in - TIME_EPS <= t < proposed.t_out + TIME_EPS:
            found = proposed
    return found


def _complement(removals: list[Removal], duration: float) -> list[tuple[float, float]]:
    gaps: list[tuple[float, float]] = []
    cursor = 0.0
    for removal in [*sorted(removals, key=lambda r: r.t_in), None]:
        end = removal.t_in if removal is not None else duration
        if end - cursor > TIME_EPS:
            gaps.append((cursor, end))
        if removal is not None:
            cursor = max(cursor, removal.t_out)
    return gaps


def _coalesce(segments: list[Segment]) -> list[Segment]:
    merged: list[Segment] = []
    for segment in segments:
        last = merged[-1] if merged else None
        if (
            last is not None
            and abs(last.t_out - segment.t_in) <= TIME_EPS
            and last.tier is segment.tier
            and last.reason == segment.reason
        ):
            merged[-1] = last.model_copy(update={"t_out": segment.t_out})
        else:
            merged.append(segment)
    return merged
