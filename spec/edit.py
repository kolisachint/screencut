"""EditDecisions — what survives (architecture.md §4.4).

Where `FocusTrack` answers "where do we look", this answers "what survives", and
under §1.1 it is the part that makes screencut an editing tool rather than a
captioning tool.

Two fields, both in **source time** like everything else in `EditSpec`, and
neither applied until `compile` (§4.5). Together they *partition* the source:
every second is either removed or in a segment, with no gaps and no overlaps.
That totality is enforced here, at the schema, because it is cheap and it catches
a whole class of model error before anything renders — half of risk R5's
mitigation, and the reason an impossible edit is unrepresentable rather than
merely detectable.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from spec.origin import Stage, spec_field
from spec.types import TIME_EPS, PositiveSeconds, Seconds, SpecModel, TimeSpan, approx_eq


class RemovalKind(str, Enum):
    """Why a range does not survive. Silence and filler are `trim`'s to find
    (§4.6, arithmetic); false starts and redundancy are `plan_edit`'s (language)."""

    SILENCE = "silence"
    FILLER = "filler"
    FALSE_START = "false_start"
    REDUNDANT = "redundant"


class Tier(str, Enum):
    """How good a segment is — aspect-independent taste, decided once (§4.4.1).

    Which tiers survive is a *separate*, per-profile, arithmetic decision driven
    by `duration_budget`. Tiers are a ranking, not a cut.
    """

    ESSENTIAL = "essential"
    SUPPORTING = "supporting"
    OPTIONAL = "optional"

    @property
    def rank(self) -> int:
        """Higher survives a tighter budget. `essential` is 2."""
        return _TIER_RANK[self]

    def at_or_above(self, threshold: "Tier") -> bool:
        return self.rank >= threshold.rank


_TIER_RANK: dict[Tier, int] = {Tier.OPTIONAL: 0, Tier.SUPPORTING: 1, Tier.ESSENTIAL: 2}

#: Loosest first. The projection rule of §4.4.1 walks this and takes the first
#: threshold that fits the budget — three tiers, three possible selections, no search.
TIER_THRESHOLDS: tuple[Tier, ...] = (Tier.OPTIONAL, Tier.SUPPORTING, Tier.ESSENTIAL)


class Removal(SpecModel, TimeSpan):
    """A range that never survives, in any profile.

    Expressed as a range rather than as rewritten text, and that is what keeps
    this an edit: audio and caption are cut at the same instants, from the same
    decision, and nothing is put into your mouth that you did not say.
    """

    t_in: Seconds = spec_field(produced_by=Stage.PLAN_EDIT)
    t_out: Seconds = spec_field(produced_by=Stage.PLAN_EDIT)
    kind: RemovalKind = spec_field(produced_by=Stage.PLAN_EDIT)
    proposed_by: Literal[Stage.TRIM, Stage.PLAN_EDIT] = spec_field(
        default=Stage.PLAN_EDIT,
        produced_by=Stage.PLAN_EDIT,
        description=(
            "Which stage first proposed this range. `trim` for an arithmetic proposal the "
            "model kept, `plan_edit` for one the model added. Feeds the trim override rate "
            "on the verification report (§9.1)."
        ),
    )

    @model_validator(mode="after")
    def _not_inverted(self) -> "Removal":
        if self.t_out <= self.t_in + TIME_EPS:
            raise ValueError(f"removal is inverted or empty: [{self.t_in}, {self.t_out}]")
        return self


class Segment(SpecModel, TimeSpan):
    """Surviving content, ranked. `reason` is what makes a tier reviewable."""

    t_in: Seconds = spec_field(produced_by=Stage.PLAN_EDIT)
    t_out: Seconds = spec_field(produced_by=Stage.PLAN_EDIT)
    tier: Tier = spec_field(produced_by=Stage.PLAN_EDIT)
    reason: Annotated[str, Field(min_length=1)] = spec_field(
        produced_by=Stage.PLAN_EDIT,
        description="Why this segment got this tier. Shown in review (§8); a tier with no reason cannot be argued with.",
    )

    @model_validator(mode="after")
    def _not_inverted(self) -> "Segment":
        if self.t_out <= self.t_in + TIME_EPS:
            raise ValueError(f"segment is inverted or empty: [{self.t_in}, {self.t_out}]")
        return self


class EditDecisions(SpecModel):
    """`removals` + `segments`, partitioning the source exactly.

    Validated here: in ascending order, non-inverted, non-overlapping, contiguous,
    and starting at 0.0. The one thing that cannot be checked here is where the
    partition *ends* — that needs the source duration, so `EditSpec` checks it
    (§4.4's totality is a two-model invariant only because duration lives on the
    source).
    """

    removals: list[Removal] = spec_field(default_factory=list, produced_by=Stage.PLAN_EDIT)
    segments: list[Segment] = spec_field(default_factory=list, produced_by=Stage.PLAN_EDIT)

    @model_validator(mode="after")
    def _partition(self) -> "EditDecisions":
        spans: list[TimeSpan] = [*self.removals, *self.segments]
        if not spans:
            return self
        ordered = sorted(spans, key=lambda s: s.t_in)
        if ordered[0].t_in > TIME_EPS:
            raise ValueError(f"edit decisions must start at source time 0.0, not {ordered[0].t_in}")
        for prev, nxt in zip(ordered, ordered[1:]):
            if nxt.t_in < prev.t_out - TIME_EPS:
                raise ValueError(f"overlapping edit decisions at [{nxt.t_in}, {prev.t_out}]")
            if nxt.t_in > prev.t_out + TIME_EPS:
                raise ValueError(f"gap in edit decisions between {prev.t_out} and {nxt.t_in}")
        return self

    @property
    def covered_until(self) -> float:
        """End of the partition. `EditSpec` requires this to equal the source duration."""
        spans: list[TimeSpan] = [*self.removals, *self.segments]
        return max((s.t_out for s in spans), default=0.0)

    def covers(self, duration: float) -> bool:
        return approx_eq(self.covered_until, duration)

    def selected(self, threshold: Tier) -> list[Segment]:
        """Segments at or above `threshold`, in source order."""
        return sorted((s for s in self.segments if s.tier.at_or_above(threshold)), key=lambda s: s.t_in)

    def selected_duration(self, threshold: Tier) -> float:
        return sum(s.duration for s in self.selected(threshold))

    def removed_duration(self) -> float:
        return sum(r.duration for r in self.removals)

    def is_removed(self, t: float) -> bool:
        return any(r.contains(t) for r in self.removals)


DEGRADED_REASON = "trim only; no editorial pass tiered this"
"""The `reason` on a segment nobody ranked.

§7.4's degradation writes real segments, so they need real reasons — a tier with
no reason cannot be argued with in review (§8), and "the model never ran" is
exactly what a reviewer needs to know about a wall of `essential`."""


def decisions_from_removals(
    removals: list[Removal],
    duration: PositiveSeconds,
    *,
    tier: Tier = Tier.ESSENTIAL,
    reason: str = DEGRADED_REASON,
) -> EditDecisions:
    """Fill the gaps between removals with segments, so the pair is total (§4.4).

    `trim` proposes removals and nothing else — what survives is not its decision
    to make. This is the arithmetic that turns a removal list into a renderable
    edit, and it has two callers that both matter: §7.4's degradation, where the
    tier is `essential` because nothing ranked it, and any future stage that needs
    the complement of a cut list.

    Everything the removals do not cover becomes one segment. Adjacent removals
    leave no segment between them rather than a zero-length one, which
    `EditDecisions` would reject and a reviewer would have to squint at.
    """
    ordered = sorted(removals, key=lambda r: r.t_in)
    segments: list[Segment] = []
    cursor = 0.0
    for removal in [*ordered, None]:
        end = removal.t_in if removal is not None else duration
        if end - cursor > TIME_EPS:
            segments.append(Segment(t_in=cursor, t_out=end, tier=tier, reason=reason))
        if removal is not None:
            cursor = max(cursor, removal.t_out)
    return EditDecisions(removals=ordered, segments=segments)


def choose_threshold(decisions: EditDecisions, budget: PositiveSeconds) -> tuple[Tier, float]:
    """The §4.4.1 projection rule: the loosest tier threshold that fits `budget`.

    Returns the threshold and the duration it selects. When `essential` alone
    overruns, `essential` is returned anyway with its (over-budget) duration —
    the profile cannot be satisfied, and that is a verification finding with a
    number attached, not a crash and not a silent overrun (§9.1).
    """
    for threshold in TIER_THRESHOLDS:  # loosest first: take as much as fits
        duration = decisions.selected_duration(threshold)
        if duration <= budget + TIME_EPS:
            return threshold, duration
    return Tier.ESSENTIAL, decisions.selected_duration(Tier.ESSENTIAL)
